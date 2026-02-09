from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, Optional

import httpx
from claude_agent_sdk import tool
from lmnr import observe

from .geo_profile import resolve_geo_profile
from .shared import ToolFailure, log_tool_call, log_tool_result, tool_error, tool_json


def _auth_headers(api_key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _extract_fmr(data: Dict[str, Any]) -> Dict[str, Any]:
    basic = data.get("basicdata") if isinstance(data, dict) else None
    if isinstance(basic, list) and basic:
        basic = basic[0]
    if not isinstance(basic, dict):
        basic = {}

    def _get(key: str) -> Optional[int]:
        value = basic.get(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    return {
        "fmr_0br": _get("fmr0"),
        "fmr_1br": _get("fmr1"),
        "fmr_2br": _get("fmr2"),
        "fmr_3br": _get("fmr3"),
        "fmr_4br": _get("fmr4"),
    }


def _classify_http_error(status_code: int) -> str:
    if status_code in {401, 403}:
        return "auth_error"
    if status_code == 429:
        return "rate_limited"
    if 400 <= status_code < 500:
        return "bad_request"
    if 500 <= status_code:
        return "upstream_error"
    return "unknown_error"


def _build_entity_id(state_fips: str | None, county_fips: str | None) -> str | None:
    if not state_fips or not county_fips:
        return None
    if len(state_fips) != 2:
        return None
    if len(county_fips) != 3:
        return None
    # HUD FMR API expects county FIPS + 99999 (example: 08014 -> 0801499999)
    return f"{state_fips}{county_fips}99999"


@tool(
    "get_hud_fmr",
    "Fetch HUD Fair Market Rent data for a ZIP code (via county FIPS).",
    {"zip_code": str, "year": int},
)
@observe()
async def get_hud_fmr(args: Dict[str, Any]) -> Dict[str, Any]:
    call_id = log_tool_call("get_hud_fmr", args)
    zip_code = (args.get("zip_code") or "").strip()
    year = args.get("year")

    api_key = (args.get("api_key") or "").strip() or None
    try:
        result = await fetch_hud_fmr(zip_code, year=year, api_key=api_key)
    except ToolFailure as exc:
        log_tool_result("get_hud_fmr", "error", call_id=call_id)
        return tool_error(exc.message, exc.details)
    except Exception as exc:  # pragma: no cover
        log_tool_result("get_hud_fmr", "error", call_id=call_id)
        return tool_error(
            "hud_fmr_request_failed",
            {"tool": "get_hud_fmr", "message": str(exc)},
        )
    log_tool_result("get_hud_fmr", "ok", call_id=call_id)
    return tool_json(result)


async def fetch_hud_fmr(
    zip_code: str,
    year: int | None = None,
    api_key: str | None = None,
) -> Dict[str, Any]:
    api_key = api_key or os.environ.get("HUD_API_KEY")
    if not api_key:
        raise ToolFailure(
            "missing_api_key",
            {"tool": "get_hud_fmr", "env": "HUD_API_KEY"},
        )

    profile = None
    try:
        profile = await resolve_geo_profile(zip_code)
        county = profile.get("county_fips")
        state = profile.get("state_fips")
        state_fips = state
        entity_id = _build_entity_id(state, county)
    except Exception:
        entity_id = None
        state_fips = None
        state = None
        county = None

    if not entity_id:
        raise ToolFailure(
            "missing_county_fips",
            {"tool": "get_hud_fmr", "zip_code": zip_code},
        )

    url = f"https://www.huduser.gov/hudapi/public/fmr/data/{entity_id}"
    params = {}
    if isinstance(year, int):
        params["year"] = year
    else:
        params["year"] = datetime.utcnow().year - 1

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(url, params=params, headers=_auth_headers(api_key))

    if response.status_code != 200:
        # If the default year failed, try the previous year once.
        if response.status_code in {400, 404} and "year" in params:
            params["year"] = params["year"] - 1
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.get(
                    url, params=params, headers=_auth_headers(api_key)
                )
            if response.status_code == 200:
                data = response.json()
                fmr = _extract_fmr(data)
                return {
                    "zip_code": zip_code,
                    "county_fips": county,
                    "state_fips": state_fips,
                    "year": params["year"],
                    "fmr": fmr,
                    "source": "HUD FMR API",
                    "note": "Raw HUD payload omitted to avoid oversized tool output.",
                }
        # Fallback to state-level FMR if county-based entity ID fails.
        fallback_state = profile.get("state") if isinstance(profile, dict) else None
        if fallback_state:
            fallback_url = (
                f"https://www.huduser.gov/hudapi/public/fmr/statedata/{fallback_state}"
            )
            async with httpx.AsyncClient(timeout=20.0) as client:
                fallback_resp = await client.get(
                    fallback_url, params=params, headers=_auth_headers(api_key)
                )
            if fallback_resp.status_code == 200:
                data = fallback_resp.json()
                fmr = _extract_fmr(data)
                return {
                    "zip_code": zip_code,
                    "county_fips": county,
                    "state_fips": state_fips,
                    "year": year,
                    "fmr": fmr,
                    "source": "HUD FMR API (state-level fallback)",
                    "note": "Raw HUD payload omitted to avoid oversized tool output.",
                }

        raise ToolFailure(
            "hud_fmr_request_failed",
            {
                "tool": "get_hud_fmr",
                "status": response.status_code,
                "error_type": _classify_http_error(response.status_code),
                "body": response.text[:500],
            },
        )

    data = response.json()
    fmr = _extract_fmr(data)

    return {
        "zip_code": zip_code,
        "county_fips": county,
        "state_fips": state_fips,
        "year": year,
        "fmr": fmr,
        "source": "HUD FMR API",
        "note": "Raw HUD payload omitted to avoid oversized tool output.",
    }
