from __future__ import annotations

import os
import asyncio
from typing import Any, Dict, List, Tuple

import httpx
from claude_agent_sdk import tool
from lmnr import observe

from .shared import ToolFailure, geocode_zipcode, log_tool_call, log_tool_result, tool_error, tool_json


OVERPASS_URL = os.environ.get("OVERPASS_URL", "https://overpass.private.coffee/api/interpreter")


CATEGORY_TO_OSM_FILTERS: Dict[str, List[Tuple[str, str]]] = {
    "restaurants": [("amenity", "restaurant")],
    "bars": [("amenity", "bar")],
    "nightlife": [("amenity", "nightclub"), ("amenity", "pub")],
    "cafes": [("amenity", "cafe")],
    "gyms": [("leisure", "fitness_centre")],
    "parks": [("leisure", "park")],
    "grocery_stores": [("shop", "supermarket"), ("shop", "convenience"), ("shop", "grocery")],
    "schools": [("amenity", "school")],
    "universities": [("amenity", "university"), ("amenity", "college")],
    "hospitals": [("amenity", "hospital")],
    "transit_stations": [
        ("railway", "station"),
        ("public_transport", "station"),
        ("amenity", "bus_station"),
    ],
    "pharmacies": [("amenity", "pharmacy")],
    "libraries": [("amenity", "library")],
    "museums": [("tourism", "museum")],
    "shopping_malls": [("shop", "mall")],
    "movie_theaters": [("amenity", "cinema")],
    "police": [("amenity", "police")],
    "fire_stations": [("amenity", "fire_station")],
    "emergency_services": [
        ("amenity", "police"),
        ("amenity", "fire_station"),
        ("amenity", "hospital"),
    ],
}

_ALIASES = {
    "restaurant": "restaurants",
    "bar": "bars",
    "night_club": "nightlife",
    "nightclub": "nightlife",
    "cafe": "cafes",
    "gym": "gyms",
    "park": "parks",
    "grocery": "grocery_stores",
    "supermarket": "grocery_stores",
    "school": "schools",
    "hospital": "hospitals",
    "transit_station": "transit_stations",
    "pharmacy": "pharmacies",
    "library": "libraries",
    "museum": "museums",
    "shopping_mall": "shopping_malls",
    "movie_theater": "movie_theaters",
    "university": "universities",
    "college": "universities",
    "police_station": "police",
    "fire_station": "fire_stations",
    "emergency": "emergency_services",
    "emergency_services": "emergency_services",
    "primary_school": "schools",
    "elementary_school": "schools",
    "middle_school": "schools",
    "high_school": "schools",
    "secondary_school": "schools",
    "kindergarten": "schools",
}


def _normalize_category(raw: str) -> str:
    return raw.strip().lower().replace(" ", "_")


def _build_query(filters: List[Tuple[str, str]], lat: float, lon: float, radius: int) -> str:
    lines: List[str] = ["[out:json][timeout:25];", "("]
    for key, value in filters:
        lines.append(f'  node(around:{radius},{lat},{lon})["{key}"="{value}"];')
        lines.append(f'  way(around:{radius},{lat},{lon})["{key}"="{value}"];')
        lines.append(f'  relation(around:{radius},{lat},{lon})["{key}"="{value}"];')
    lines.append(");")
    lines.append("out center;")
    return "\n".join(lines)


def _build_count_query(filters: List[Tuple[str, str]], lat: float, lon: float, radius: int) -> str:
    lines: List[str] = ["[out:json][timeout:20];", "("]
    for key, value in filters:
        lines.append(f'  node(around:{radius},{lat},{lon})["{key}"="{value}"];')
        lines.append(f'  way(around:{radius},{lat},{lon})["{key}"="{value}"];')
        lines.append(f'  relation(around:{radius},{lat},{lon})["{key}"="{value}"];')
    lines.append(");")
    lines.append("out count;")
    return "\n".join(lines)


def _extract_names(elements: List[Dict[str, Any]]) -> List[str]:
    names = []
    for element in elements:
        tags = element.get("tags") or {}
        name = tags.get("name")
        if name:
            names.append(name)
    return names


def _extract_count(elements: List[Dict[str, Any]]) -> int:
    if not elements:
        return 0
    tags = (elements[0] or {}).get("tags") or {}
    total = tags.get("total") or tags.get("nodes") or 0
    try:
        return int(total)
    except (TypeError, ValueError):
        return 0


_CACHE: Dict[Tuple[float, float, str, int], Dict[str, Any]] = {}
_COUNT_ONLY_CATEGORIES = {"police", "fire_stations", "hospitals", "emergency_services"}


async def _query_overpass(query: str, max_retries: int = 1) -> Dict[str, Any]:
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(OVERPASS_URL, params={"data": query})
            if response.status_code == 200:
                return response.json()
            if response.status_code in {429, 504} and attempt < max_retries:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            raise ToolFailure(
                "overpass_request_failed",
                {
                    "tool": "search_overpass_amenities",
                    "status": response.status_code,
                    "body": response.text[:500],
                },
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            if attempt < max_retries:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            raise ToolFailure(
                "overpass_request_failed",
                {"tool": "search_overpass_amenities", "message": str(exc)},
            ) from exc
    raise ToolFailure(
        "overpass_request_failed",
        {"tool": "search_overpass_amenities", "message": "max_retries_exceeded"},
    )


async def fetch_overpass_amenities(
    lat: float,
    lon: float,
    category: str,
    radius_meters: int = 1500,
) -> Dict[str, Any]:
    normalized = _normalize_category(category)
    canonical = _ALIASES.get(normalized) or normalized
    filters = CATEGORY_TO_OSM_FILTERS.get(canonical)
    if not filters:
        raise ToolFailure(
            "unsupported_category",
            {
                "tool": "search_osm_amenities",
                "category": category,
                "supported": sorted(CATEGORY_TO_OSM_FILTERS.keys()),
            },
        )

    cache_key = (round(lat, 5), round(lon, 5), canonical, radius_meters)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    radii = [radius_meters]
    if radius_meters > 800:
        radii.append(800)
    if radius_meters > 500:
        radii.append(500)

    data = None
    last_error: ToolFailure | None = None
    for radius in radii:
        try:
            if canonical in _COUNT_ONLY_CATEGORIES:
                count_radius = min(radius, 800)
                query = _build_count_query(filters, lat, lon, count_radius)
                data = await _query_overpass(query, max_retries=1)
                count = _extract_count(data.get("elements") or [])
                result = {
                    "category": canonical,
                    "count": count,
                    "sample_names": [],
                    "radius_meters": count_radius,
                    "source": OVERPASS_URL,
                    "note": "Counts only (fast query) for safety infrastructure.",
                }
                cache_key = (round(lat, 5), round(lon, 5), canonical, count_radius)
                _CACHE[cache_key] = result
                _CACHE[(round(lat, 5), round(lon, 5), canonical, radius)] = result
                return result
            query = _build_query(filters, lat, lon, radius)
            data = await _query_overpass(query, max_retries=1)
            radius_meters = radius
            break
        except ToolFailure as exc:
            last_error = exc
            details = exc.details or {}
            status = details.get("status")
            if status in {429, 504}:
                continue
            raise
    if data is None and last_error:
        raise last_error
    elements = data.get("elements") if isinstance(data, dict) else None
    if not isinstance(elements, list):
        raise ToolFailure(
            "overpass_invalid_response",
            {"tool": "search_overpass_amenities"},
        )

    names = _extract_names(elements)
    cache_key = (round(lat, 5), round(lon, 5), canonical, radius_meters)
    result = {
        "category": canonical,
        "count": len(elements),
        "sample_names": names[:5],
        "radius_meters": radius_meters,
        "source": OVERPASS_URL,
        "note": "OSM amenities provide counts only (no ratings).",
    }
    _CACHE[cache_key] = result
    return result


async def fetch_osm_amenities(zip_code: str, category: str, radius_meters: int = 1500) -> Dict[str, Any]:
    geo = await geocode_zipcode(zip_code)
    result = await fetch_overpass_amenities(
        geo["latitude"],
        geo["longitude"],
        category,
        radius_meters=radius_meters,
    )
    result["zip_code"] = zip_code
    result["center"] = {"latitude": geo["latitude"], "longitude": geo["longitude"]}
    return result


@tool(
    "search_osm_amenities",
    "Count amenities using OpenStreetMap Overpass API (no ratings).",
    {"zip_code": str, "category": str, "radius_meters": int},
)
@observe()
async def search_osm_amenities(args: Dict[str, Any]) -> Dict[str, Any]:
    call_id = log_tool_call("search_osm_amenities", args)
    zip_code = (args.get("zip_code") or "").strip()
    category = (args.get("category") or "").strip()
    radius = int(args.get("radius_meters") or 1500)

    try:
        result = await fetch_osm_amenities(zip_code, category, radius_meters=radius)
    except ToolFailure as exc:
        log_tool_result("search_osm_amenities", "error", call_id=call_id)
        return tool_error(exc.message, exc.details)
    except Exception as exc:  # pragma: no cover
        log_tool_result("search_osm_amenities", "error", call_id=call_id)
        return tool_error(
            "overpass_request_failed",
            {"tool": "search_osm_amenities", "message": str(exc)},
        )

    log_tool_result("search_osm_amenities", "ok", call_id=call_id)
    return tool_json(result)


@tool(
    "search_overpass_amenities",
    "Count amenities using OpenStreetMap Overpass API by coordinates.",
    {"lat": float, "lon": float, "category": str, "radius_meters": int},
)
@observe()
async def search_overpass_amenities(args: Dict[str, Any]) -> Dict[str, Any]:
    call_id = log_tool_call("search_overpass_amenities", args)
    lat = args.get("lat")
    lon = args.get("lon")
    category = (args.get("category") or "").strip()
    radius = int(args.get("radius_meters") or 1500)

    if lat is None or lon is None:
        log_tool_result("search_overpass_amenities", "error", call_id=call_id)
        return tool_error(
            "missing_coordinates",
            {"tool": "search_overpass_amenities", "lat": lat, "lon": lon},
        )

    try:
        result = await fetch_overpass_amenities(lat, lon, category, radius_meters=radius)
    except ToolFailure as exc:
        log_tool_result("search_overpass_amenities", "error", call_id=call_id)
        return tool_error(exc.message, exc.details)
    except Exception as exc:  # pragma: no cover
        log_tool_result("search_overpass_amenities", "error", call_id=call_id)
        return tool_error(
            "overpass_request_failed",
            {"tool": "search_overpass_amenities", "message": str(exc)},
        )

    log_tool_result("search_overpass_amenities", "ok", call_id=call_id)
    return tool_json(result)
