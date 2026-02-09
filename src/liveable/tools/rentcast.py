from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import httpx
from claude_agent_sdk import tool
from lmnr import observe

from .shared import ToolFailure, log_tool_call, log_tool_result, tool_error, tool_json


RENTCAST_BASE_URL = "https://api.rentcast.io/v1"


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


def _require_api_key(api_key: Optional[str]) -> str:
    api_key = api_key or os.environ.get("RENTCAST_API_KEY")
    if not api_key:
        raise ToolFailure(
            "missing_api_key",
            {"tool": "get_rentcast_market", "env": "RENTCAST_API_KEY"},
        )
    return api_key


def _truncate_payload(payload: Any, max_items: int = 50, depth: int = 2) -> Any:
    if depth <= 0:
        return payload
    if isinstance(payload, list):
        return payload[:max_items]
    if isinstance(payload, dict):
        trimmed: Dict[str, Any] = {}
        for key, value in payload.items():
            if isinstance(value, list):
                trimmed[key] = value[:max_items]
            elif isinstance(value, dict):
                trimmed[key] = _truncate_payload(value, max_items=max_items, depth=depth - 1)
            else:
                trimmed[key] = value
        return trimmed
    return payload


def _summarize_listing(listing: Dict[str, Any]) -> Dict[str, Any]:
    keys = [
        "address",
        "city",
        "state",
        "zipCode",
        "price",
        "listPrice",
        "beds",
        "baths",
        "squareFootage",
        "pricePerSquareFoot",
        "daysOnMarket",
        "propertyType",
        "yearBuilt",
    ]
    summary: Dict[str, Any] = {}
    for key in keys:
        if key in listing and listing[key] is not None:
            summary[key] = listing[key]
    return summary if summary else listing


def _summarize_market(market: Dict[str, Any]) -> Dict[str, Any]:
    keep = [
        "medianSalePrice",
        "medianListPrice",
        "medianRent",
        "averageRent",
        "averageSalePrice",
        "pricePerSquareFoot",
        "rentYoY",
        "priceYoY",
        "marketScore",
        "marketTemperature",
        "daysOnMarket",
        "inventory",
        "lastUpdated",
    ]
    summary: Dict[str, Any] = {}
    for key in keep:
        if key in market and market[key] is not None:
            summary[key] = market[key]
    if not summary:
        for key, value in market.items():
            if isinstance(value, (str, int, float, bool)) and len(summary) < 20:
                summary[key] = value
    return summary


async def _request(endpoint: str, params: Dict[str, Any], api_key: str) -> Dict[str, Any]:
    headers = {
        "X-Api-Key": api_key,
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(f"{RENTCAST_BASE_URL}{endpoint}", params=params, headers=headers)
    if response.status_code != 200:
        raise ToolFailure(
            "rentcast_request_failed",
            {
                "tool": "rentcast",
                "status": response.status_code,
                "error_type": _classify_http_error(response.status_code),
                "body": response.text[:500],
            },
        )
    return response.json()


async def fetch_rentcast_market(
    zip_code: str,
    data_type: str | None = None,
    history_months: int | None = None,
    api_key: str | None = None,
) -> Dict[str, Any]:
    api_key = _require_api_key(api_key)
    params: Dict[str, Any] = {"zipCode": zip_code}
    if data_type:
        params["dataType"] = data_type
    if history_months:
        params["historyRange"] = history_months
    return await _request("/markets", params, api_key)


async def fetch_rentcast_sale_listings(
    zip_code: str,
    limit: int = 50,
    api_key: str | None = None,
) -> Dict[str, Any]:
    api_key = _require_api_key(api_key)
    params: Dict[str, Any] = {"zipCode": zip_code, "limit": limit}
    return await _request("/listings/sale", params, api_key)


@tool(
    "get_rentcast_market",
    "Fetch RentCast market statistics for a ZIP code.",
    {"zip_code": str, "data_type": str, "history_months": int},
)
@observe()
async def get_rentcast_market(args: Dict[str, Any]) -> Dict[str, Any]:
    call_id = log_tool_call("get_rentcast_market", args)
    zip_code = (args.get("zip_code") or "").strip()
    data_type = (args.get("data_type") or "").strip() or None
    history_months = args.get("history_months")
    api_key = (args.get("api_key") or "").strip() or None

    try:
        result = await fetch_rentcast_market(
            zip_code,
            data_type=data_type,
            history_months=history_months,
            api_key=api_key,
        )
    except ToolFailure as exc:
        log_tool_result("get_rentcast_market", "error", call_id=call_id)
        return tool_error(exc.message, exc.details)
    except Exception as exc:  # pragma: no cover
        log_tool_result("get_rentcast_market", "error", call_id=call_id)
        return tool_error(
            "rentcast_request_failed",
            {"tool": "get_rentcast_market", "message": str(exc)},
        )

    log_tool_result("get_rentcast_market", "ok", call_id=call_id)
    market_data = result if isinstance(result, dict) else {}
    if isinstance(result, dict) and "market" in result and isinstance(result["market"], dict):
        market_data = result["market"]
    return tool_json(
        {
            "zip_code": zip_code,
            "market": _summarize_market(market_data),
            "source": RENTCAST_BASE_URL,
            "note": "Market payload summarized to avoid oversized responses.",
        }
    )


@tool(
    "get_rentcast_sale_listings",
    "Fetch RentCast sale listings for a ZIP code.",
    {"zip_code": str, "limit": int},
)
@observe()
async def get_rentcast_sale_listings(args: Dict[str, Any]) -> Dict[str, Any]:
    call_id = log_tool_call("get_rentcast_sale_listings", args)
    zip_code = (args.get("zip_code") or "").strip()
    limit = int(args.get("limit") or 50)
    api_key = (args.get("api_key") or "").strip() or None

    try:
        result = await fetch_rentcast_sale_listings(
            zip_code,
            limit=limit,
            api_key=api_key,
        )
    except ToolFailure as exc:
        log_tool_result("get_rentcast_sale_listings", "error", call_id=call_id)
        return tool_error(exc.message, exc.details)
    except Exception as exc:  # pragma: no cover
        log_tool_result("get_rentcast_sale_listings", "error", call_id=call_id)
        return tool_error(
            "rentcast_request_failed",
            {"tool": "get_rentcast_sale_listings", "message": str(exc)},
        )

    log_tool_result("get_rentcast_sale_listings", "ok", call_id=call_id)
    listings = []
    if isinstance(result, list):
        listings = result
    elif isinstance(result, dict):
        listings = result.get("listings") or result.get("data") or []
    summaries = [_summarize_listing(item) for item in listings[:20] if isinstance(item, dict)]
    return tool_json(
        {
            "zip_code": zip_code,
            "listings": summaries,
            "source": RENTCAST_BASE_URL,
            "note": "Listings summarized to avoid oversized responses.",
        }
    )
