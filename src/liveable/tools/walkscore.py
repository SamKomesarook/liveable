from __future__ import annotations

import os
from typing import Any, Dict

import httpx
from claude_agent_sdk import tool
from lmnr import observe

from .shared import ToolFailure, log_tool_call, log_tool_result, tool_error, tool_json


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


@tool(
    "get_walkscore",
    "Get Walk Score, Transit Score, and Bike Score for a coordinate.",
    {"lat": float, "lon": float, "address": str},
)
@observe()
async def get_walkscore(args: Dict[str, Any]) -> Dict[str, Any]:
    call_id = log_tool_call("get_walkscore", args)
    lat = args.get("lat")
    lon = args.get("lon")
    address = (args.get("address") or "").strip()

    api_key = (args.get("api_key") or "").strip() or None
    try:
        result = await fetch_walkscore(lat, lon, address, api_key=api_key)
    except ToolFailure as exc:
        log_tool_result("get_walkscore", "error", call_id=call_id)
        return tool_error(exc.message, exc.details)
    except Exception as exc:  # pragma: no cover
        log_tool_result("get_walkscore", "error", call_id=call_id)
        return tool_error(
            "walkscore_request_failed",
            {"tool": "get_walkscore", "message": str(exc)},
        )

    log_tool_result("get_walkscore", "ok", call_id=call_id)
    return tool_json(result)


async def fetch_walkscore(
    lat: float | None,
    lon: float | None,
    address: str,
    api_key: str | None = None,
) -> Dict[str, Any]:
    api_key = api_key or os.environ.get("WALKSCORE_API_KEY")
    if not api_key:
        raise ToolFailure(
            "missing_api_key",
            {"tool": "get_walkscore", "env": "WALKSCORE_API_KEY"},
        )

    if lat is None or lon is None:
        raise ToolFailure(
            "missing_coordinates",
            {"tool": "get_walkscore", "lat": lat, "lon": lon},
        )

    params = {
        "format": "json",
        "address": address,
        "lat": lat,
        "lon": lon,
        "transit": 1,
        "bike": 1,
        "wsapikey": api_key,
    }

    url = "https://api.walkscore.com/score"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url, params=params)

    if response.status_code != 200:
        raise ToolFailure(
            "walkscore_request_failed",
            {
                "tool": "get_walkscore",
                "status": response.status_code,
                "error_type": _classify_http_error(response.status_code),
                "body": response.text[:500],
            },
        )

    data = response.json()
    if data.get("status") != 1:
        raise ToolFailure(
            "walkscore_no_data", {"tool": "get_walkscore", "response": data}
        )

    return {
        "walkscore": data.get("walkscore"),
        "description": data.get("description"),
        "transit_score": (data.get("transit") or {}).get("score"),
        "bike_score": (data.get("bike") or {}).get("score"),
    }
