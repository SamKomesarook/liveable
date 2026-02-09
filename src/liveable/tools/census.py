from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

import httpx
from claude_agent_sdk import tool
from lmnr import observe

from .shared import ToolFailure, log_tool_call, log_tool_result, tool_error, tool_json
from .geo_profile import resolve_geo_profile


COMMUTE_BUCKETS: List[Tuple[str, float]] = [
    ("B08303_002E", 2.5),
    ("B08303_003E", 7.0),
    ("B08303_004E", 12.0),
    ("B08303_005E", 17.0),
    ("B08303_006E", 22.0),
    ("B08303_007E", 27.0),
    ("B08303_008E", 32.0),
    ("B08303_009E", 37.0),
    ("B08303_010E", 42.0),
    ("B08303_011E", 52.0),
    ("B08303_012E", 75.0),
    ("B08303_013E", 100.0),
]


def _classify_http_error(status_code: int) -> str:
    if status_code == 204:
        return "no_data"
    if status_code in {401, 403}:
        return "auth_error"
    if status_code == 429:
        return "rate_limited"
    if 400 <= status_code < 500:
        return "bad_request"
    if 500 <= status_code:
        return "upstream_error"
    return "unknown_error"


async def fetch_census_demographics(
    zip_code: str, api_key: str | None = None
) -> Dict[str, Any]:
    api_key = api_key or os.environ.get("CENSUS_API_KEY")
    if not api_key:
        raise ToolFailure(
            "missing_api_key",
            {"tool": "get_census_demographics", "env": "CENSUS_API_KEY"},
        )

    variables = [
        "B01003_001E",  # population
        "B19013_001E",  # median household income
        "B01002_001E",  # median age
        "B25003_001E",  # occupancy total
        "B25003_002E",  # owner occupied
        "B17001_001E",  # poverty universe
        "B17001_002E",  # below poverty
        "B15003_001E",  # education total
        "B15003_022E",  # bachelor's
        "B15003_023E",  # master's
        "B15003_024E",  # professional
        "B15003_025E",  # doctorate
        "B25064_001E",  # median rent
        "B08303_001E",  # commute total
    ] + [bucket for bucket, _ in COMMUTE_BUCKETS]

    url = "https://api.census.gov/data/2022/acs/acs5"

    async def _fetch(params: Dict[str, str]) -> httpx.Response:
        async with httpx.AsyncClient(timeout=15.0) as client:
            return await client.get(url, params=params)

    params = {
        "get": ",".join(variables),
        "for": f"zip code tabulation area:{zip_code}",
        "key": api_key,
    }

    response = await _fetch(params)

    geography = "zip"
    geography_fips = None

    if response.status_code != 200:
        if response.status_code != 204:
            raise ToolFailure(
                "census_request_failed",
                {
                    "tool": "get_census_demographics",
                    "status": response.status_code,
                    "error_type": _classify_http_error(response.status_code),
                    "body": response.text[:500],
                },
            )

        # For 204 (no ZCTA), attempt county fallback below.
        payload = []
    else:
        payload = response.json()

    if not payload or len(payload) < 2:
        # Try county fallback if ZIP has no ZCTA.
        try:
            profile = await resolve_geo_profile(zip_code)
            state_fips = profile.get("state_fips")
            county_fips = profile.get("county_fips")
        except Exception:
            state_fips = None
            county_fips = None

        if state_fips and county_fips:
            county_params = {
                "get": ",".join(variables),
                "for": f"county:{county_fips}",
                "in": f"state:{state_fips}",
                "key": api_key,
            }
            county_resp = await _fetch(county_params)
            if county_resp.status_code == 200:
                county_payload = county_resp.json()
                if county_payload and len(county_payload) >= 2:
                    header, values = county_payload[0], county_payload[1]
                    geography = "county"
                    geography_fips = f"{state_fips}{county_fips}"
                    payload = [header, values]
                else:
                    raise ToolFailure(
                        "census_no_data",
                        {
                            "tool": "get_census_demographics",
                            "zip_code": zip_code,
                            "fallback": "county",
                        },
                    )
            else:
                raise ToolFailure(
                    "census_request_failed",
                    {
                        "tool": "get_census_demographics",
                        "status": county_resp.status_code,
                        "error_type": _classify_http_error(county_resp.status_code),
                        "fallback": "county",
                        "body": county_resp.text[:500],
                    },
                )
        else:
            raise ToolFailure(
                "census_no_data",
                {"tool": "get_census_demographics", "zip_code": zip_code},
            )

    header, values = payload[0], payload[1]
    data = dict(zip(header, values))

    MISSING_SENTINELS = {"-666666666", "-666666666.0", -666666666, -666666666.0}

    def _is_missing(value: Any) -> bool:
        return value in MISSING_SENTINELS

    def _to_int(key: str) -> int | None:
        value = data.get(key)
        if _is_missing(value):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _to_float(key: str) -> float | None:
        value = data.get(key)
        if _is_missing(value):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    population = _to_int("B01003_001E")
    median_income = _to_int("B19013_001E")
    median_age = _to_float("B01002_001E")
    median_rent = _to_int("B25064_001E")

    owner_total = _to_int("B25003_001E") or 0
    owner_occ = _to_int("B25003_002E") or 0
    pct_owner_occupied = (owner_occ / owner_total * 100) if owner_total else None

    poverty_total = _to_int("B17001_001E") or 0
    poverty_below = _to_int("B17001_002E") or 0
    poverty_rate = (poverty_below / poverty_total * 100) if poverty_total else None

    edu_total = _to_int("B15003_001E") or 0
    edu_bachelor = _to_int("B15003_022E") or 0
    edu_master = _to_int("B15003_023E") or 0
    edu_prof = _to_int("B15003_024E") or 0
    edu_phd = _to_int("B15003_025E") or 0
    pct_college_educated = (
        (edu_bachelor + edu_master + edu_prof + edu_phd) / edu_total * 100
        if edu_total
        else None
    )

    commute_total = _to_int("B08303_001E") or 0
    commute_weighted = 0.0
    commute_count = 0
    for bucket_key, midpoint in COMMUTE_BUCKETS:
        count = _to_int(bucket_key) or 0
        commute_weighted += count * midpoint
        commute_count += count
    commute_time_avg = (
        commute_weighted / commute_count if commute_count else None
    )

    result = {
        "zip_code": zip_code,
        "population": population,
        "median_household_income": median_income,
        "median_age": median_age,
        "pct_college_educated": pct_college_educated,
        "pct_owner_occupied": pct_owner_occupied,
        "commute_time_avg": commute_time_avg,
        "poverty_rate": poverty_rate,
        "median_rent": median_rent,
    }

    if geography != "zip":
        result["geography"] = geography
        result["geography_fips"] = geography_fips

    return result


@tool(
    "get_census_demographics",
    "Get ACS 5-year demographics for a ZIP code.",
    {"zip_code": str},
)
@observe()
async def get_census_demographics(args: Dict[str, Any]) -> Dict[str, Any]:
    call_id = log_tool_call("get_census_demographics", args)
    zip_code = (args.get("zip_code") or "").strip()
    api_key = (args.get("api_key") or "").strip() or None
    try:
        result = await fetch_census_demographics(zip_code, api_key=api_key)
    except ToolFailure as exc:
        log_tool_result("get_census_demographics", "error", call_id=call_id)
        return tool_error(exc.message, exc.details)
    except Exception as exc:  # pragma: no cover
        log_tool_result("get_census_demographics", "error", call_id=call_id)
        return tool_error(
            "census_request_failed",
            {"tool": "get_census_demographics", "message": str(exc)},
        )
    log_tool_result("get_census_demographics", "ok", call_id=call_id)
    return tool_json(result)
