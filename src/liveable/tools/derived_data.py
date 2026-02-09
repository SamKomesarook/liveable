from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import httpx
from claude_agent_sdk import tool
from lmnr import observe

from .hud_fmr import fetch_hud_fmr
from .rentcast import fetch_rentcast_market, fetch_rentcast_sale_listings
from .shared import ToolFailure, log_tool_call, log_tool_result, tool_error, tool_json


def _pick_first(d: Dict[str, Any], keys: List[str]) -> Optional[Any]:
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return None


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2 == 1:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def _extract_price_candidates(item: Dict[str, Any], keys: List[str]) -> Optional[float]:
    for key in keys:
        if key in item and item[key] not in (None, ""):
            try:
                return float(item[key])
            except (TypeError, ValueError):
                continue
    return None


async def fetch_housing_prices(zip_code: str, year: int | None = None) -> Dict[str, Any]:
    rentcast_market = None
    rentcast_listings = None
    try:
        rentcast_market = await fetch_rentcast_market(zip_code)
    except ToolFailure:
        rentcast_market = None
    try:
        rentcast_listings = await fetch_rentcast_sale_listings(zip_code, limit=50)
    except ToolFailure:
        rentcast_listings = None

    median_home_price = None
    price_per_sqft = None
    median_rent = None
    yoy_change = None
    sources: List[str] = []

    listings: List[Dict[str, Any]] = []
    if isinstance(rentcast_listings, list):
        listings = rentcast_listings
    elif isinstance(rentcast_listings, dict):
        listings = rentcast_listings.get("listings") or []

    if listings:
        prices = []
        ppsf = []
        for listing in listings:
            price = _extract_price_candidates(
                listing,
                ["listPrice", "price", "listingPrice", "list_price"],
            )
            if price is not None:
                prices.append(price)
            price_sqft = _extract_price_candidates(
                listing,
                ["pricePerSquareFoot", "price_per_sqft", "pricePerSqft"],
            )
            if price_sqft is not None:
                ppsf.append(price_sqft)
        median_home_price = _median(prices)
        price_per_sqft = _median(ppsf)
        if median_home_price or price_per_sqft:
            sources.append("RentCast listings")

    if rentcast_market and isinstance(rentcast_market, dict):
        median_rent = _pick_first(
            rentcast_market,
            ["medianRent", "medianRentPrice", "averageRent", "averageRentPrice"],
        )
        if median_rent is None and isinstance(rentcast_market.get("rent"), dict):
            median_rent = _pick_first(
                rentcast_market["rent"],
                ["median", "average", "medianRent"],
            )
        if yoy_change is None:
            yoy_change = _pick_first(
                rentcast_market,
                ["rentYoY", "priceYoY", "yoyChange"],
            )
        if "RentCast market" not in sources:
            sources.append("RentCast market")

    if median_rent is None:
        hud_payload = await fetch_hud_fmr(zip_code, year=year)
        fmr = hud_payload.get("fmr") if isinstance(hud_payload.get("fmr"), dict) else {}
        median_rent = _pick_first(hud_payload, ["fmr_2br", "fmr_1br"])
        if median_rent is None:
            median_rent = _pick_first(fmr, ["fmr_2br", "fmr_1br"])
        sources.append(hud_payload.get("source") or "HUD FMR API")

    return {
        "zip_code": zip_code,
        "median_home_price": median_home_price,
        "median_rent": median_rent,
        "yoy_change": yoy_change,
        "price_per_sqft": price_per_sqft,
        "source": ", ".join(sorted(set(sources))) if sources else "HUD FMR API",
        "note": (
            "Home price data uses listings where available; rent uses RentCast or HUD FMR."
        ),
    }


@tool(
    "search_housing_prices",
    "Return housing rent benchmarks using HUD FMR (no web search).",
    {"zip_code": str, "year": int},
)
@observe()
async def search_housing_prices(args: Dict[str, Any]) -> Dict[str, Any]:
    call_id = log_tool_call("search_housing_prices", args)
    zip_code = (args.get("zip_code") or "").strip()
    year = args.get("year")

    try:
        result = await fetch_housing_prices(zip_code, year=year)
    except ToolFailure as exc:
        log_tool_result("search_housing_prices", "error", call_id=call_id)
        return tool_error(
            "housing_data_unavailable",
            {"tool": "search_housing_prices", "details": exc.details},
        )
    except Exception as exc:  # pragma: no cover
        log_tool_result("search_housing_prices", "error", call_id=call_id)
        return tool_error(
            "housing_data_unavailable",
            {"tool": "search_housing_prices", "message": str(exc)},
        )

    log_tool_result("search_housing_prices", "ok", call_id=call_id)
    return tool_json(result)




async def fetch_new_developments(zip_code: str, city: str = "") -> Dict[str, Any]:
    base_url = os.environ.get("DEV_PERMITS_BASE_URL")
    if not base_url:
        raise ToolFailure(
            "developments_not_configured",
            {
                "tool": "search_new_developments",
                "message": "Set DEV_PERMITS_BASE_URL to a Socrata/ArcGIS open data endpoint.",
            },
        )

    query_template = os.environ.get("DEV_PERMITS_QUERY")
    params: Dict[str, Any] = {"$limit": 10}
    if query_template:
        params["$where"] = query_template.format(zip_code=zip_code, city=city)

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(base_url, params=params)

    if response.status_code != 200:
        raise ToolFailure(
            "developments_request_failed",
            {
                "tool": "search_new_developments",
                "status": response.status_code,
                "body": response.text[:500],
            },
        )

    data = response.json()
    if not isinstance(data, list):
        raise ToolFailure(
            "developments_invalid_response",
            {"tool": "search_new_developments"},
        )

    def _field(item: Dict[str, Any], keys: List[str]) -> Optional[Any]:
        for key in keys:
            if key in item and item[key]:
                return item[key]
        return None

    developments = []
    for item in data[:10]:
        developments.append(
            {
                "name": _field(item, ["project_name", "name", "description"]) or "Unknown",
                "type": _field(item, ["permit_type", "type", "category"]),
                "status": _field(item, ["status", "permit_status"]),
                "estimated_completion": _field(item, ["completion_date", "estimated_completion"]),
                "description": _field(item, ["description", "details", "scope"]),
                "address": _field(item, ["address", "location", "site_address"]),
            }
        )

    return {
        "zip_code": zip_code,
        "city": city or None,
        "developments": developments,
        "trend_summary": None,
        "source": base_url,
    }


@tool(
    "search_new_developments",
    "Fetch development/permit records from a configured open data endpoint.",
    {"zip_code": str, "city": str},
)
@observe()
async def search_new_developments(args: Dict[str, Any]) -> Dict[str, Any]:
    call_id = log_tool_call("search_new_developments", args)
    zip_code = (args.get("zip_code") or "").strip()
    city = (args.get("city") or "").strip()

    try:
        result = await fetch_new_developments(zip_code, city=city)
    except ToolFailure as exc:
        log_tool_result("search_new_developments", "error", call_id=call_id)
        return tool_error(
            "developments_request_failed",
            {"tool": "search_new_developments", "details": exc.details},
        )
    except Exception as exc:  # pragma: no cover
        log_tool_result("search_new_developments", "error", call_id=call_id)
        return tool_error(
            "developments_request_failed",
            {"tool": "search_new_developments", "message": str(exc)},
        )

    log_tool_result("search_new_developments", "ok", call_id=call_id)
    return tool_json(result)
