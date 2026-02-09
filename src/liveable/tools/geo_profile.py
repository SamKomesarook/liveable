from __future__ import annotations

from typing import Any, Dict, Optional

import httpx
from claude_agent_sdk import tool
from lmnr import observe

from .shared import geocode_zipcode, log_tool_call, log_tool_result, tool_error, tool_json


async def _reverse_geocode(lat: float, lon: float) -> Dict[str, Any]:
    url = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
    params = {
        "x": lon,
        "y": lat,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "format": "json",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url, params=params)

    if response.status_code != 200:
        raise ValueError(f"Census geocoder failed with status {response.status_code}")

    return response.json()


def _pick_first(geographies: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
    value = geographies.get(key)
    if isinstance(value, list) and value:
        return value[0]
    return None


async def resolve_geo_profile(zip_code: str) -> Dict[str, Any]:
    base = await geocode_zipcode(zip_code)
    geo = await _reverse_geocode(base["latitude"], base["longitude"])
    geographies = (geo.get("result") or {}).get("geographies") or {}
    county = _pick_first(geographies, "Counties")
    tract = _pick_first(geographies, "Census Tracts")
    cbsa = _pick_first(geographies, "Metropolitan Statistical Areas")

    return {
        "zip_code": zip_code,
        "city": base.get("city"),
        "state": base.get("state"),
        "latitude": base.get("latitude"),
        "longitude": base.get("longitude"),
        "county_name": county.get("NAME") if county else None,
        "county_fips": county.get("COUNTY") if county else None,
        "state_fips": county.get("STATE") if county else None,
        "tract_geoid": tract.get("GEOID") if tract else None,
        "tract_name": tract.get("NAME") if tract else None,
        "cbsa_code": cbsa.get("CBSA") if cbsa else None,
        "cbsa_title": cbsa.get("NAME") if cbsa else None,
        "source": "Census Geocoder",
    }


@tool(
    "get_geo_profile",
    "Resolve ZIP code to city/state, county, tract, and metro identifiers.",
    {"zip_code": str},
)
@observe()
async def get_geo_profile(args: Dict[str, Any]) -> Dict[str, Any]:
    call_id = log_tool_call("get_geo_profile", args)
    zip_code = (args.get("zip_code") or "").strip()
    try:
        result = await resolve_geo_profile(zip_code)
    except Exception as exc:
        log_tool_result("get_geo_profile", "error", call_id=call_id)
        return tool_error(
            "geo_profile_failed",
            {"tool": "get_geo_profile", "message": str(exc)},
        )

    log_tool_result("get_geo_profile", "ok", call_id=call_id)
    return tool_json(result)
