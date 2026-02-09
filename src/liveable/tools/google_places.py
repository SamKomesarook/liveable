from __future__ import annotations

import os
import re
import statistics
from typing import Any, Dict, List

import httpx
from claude_agent_sdk import tool
from lmnr import observe

from .overpass_amenities import fetch_overpass_amenities
from .shared import geocode_zipcode, log_tool_call, log_tool_result, tool_error, tool_json


CATEGORY_TO_PLACE_TYPE = {
    "restaurants": "restaurant",
    "bars": "bar",
    "nightlife": "night_club",
    "cafes": "cafe",
    "gyms": "gym",
    "parks": "park",
    "grocery_stores": "grocery_store",
    "schools": "school",
    "hospitals": "hospital",
    "transit_stations": "transit_station",
    "pharmacies": "pharmacy",
    "libraries": "library",
    "museums": "museum",
    "shopping_malls": "shopping_mall",
    "movie_theaters": "movie_theater",
}

_CATEGORY_ALIASES = {
    "restaurant": "restaurants",
    "restaurants": "restaurants",
    "bar": "bars",
    "bars": "bars",
    "night_club": "nightlife",
    "night_clubs": "nightlife",
    "nightclub": "nightlife",
    "nightclubs": "nightlife",
    "nightlife": "nightlife",
    "cafe": "cafes",
    "cafes": "cafes",
    "coffee": "cafes",
    "coffee_shop": "cafes",
    "coffee_shops": "cafes",
    "gym": "gyms",
    "gyms": "gyms",
    "fitness": "gyms",
    "fitness_center": "gyms",
    "park": "parks",
    "parks": "parks",
    "trail": "parks",
    "trails": "parks",
    "grocery": "grocery_stores",
    "grocery_store": "grocery_stores",
    "grocery_stores": "grocery_stores",
    "supermarket": "grocery_stores",
    "supermarkets": "grocery_stores",
    "grocery_or_supermarket": "grocery_stores",
    "market": "grocery_stores",
    "markets": "grocery_stores",
    "school": "schools",
    "schools": "schools",
    "primary_school": "schools",
    "elementary_school": "schools",
    "middle_school": "schools",
    "high_school": "schools",
    "secondary_school": "schools",
    "kindergarten": "schools",
    "hospital": "hospitals",
    "hospitals": "hospitals",
    "medical_center": "hospitals",
    "transit": "transit_stations",
    "transit_station": "transit_stations",
    "transit_stations": "transit_stations",
    "train_station": "transit_stations",
    "bus_station": "transit_stations",
    "subway_station": "transit_stations",
    "pharmacy": "pharmacies",
    "pharmacies": "pharmacies",
    "drugstore": "pharmacies",
    "library": "libraries",
    "libraries": "libraries",
    "museum": "museums",
    "museums": "museums",
    "shopping_mall": "shopping_malls",
    "shopping_malls": "shopping_malls",
    "mall": "shopping_malls",
    "malls": "shopping_malls",
    "movie_theater": "movie_theaters",
    "movie_theaters": "movie_theaters",
    "cinema": "movie_theaters",
    "cinemas": "movie_theaters",
    "art": "museums",
    "arts": "museums",
    "entertainment": "museums",
    "food": "restaurants",
    "shopping": "grocery_stores",
    "health": "hospitals",
}


def _normalize_category(raw: str) -> str:
    text = raw.lower().replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text


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
    "search_nearby_amenities",
    "Search nearby amenities using Google Places Nearby Search.",
    {"zip_code": str, "category": str, "radius_meters": int},
)
@observe()
async def search_nearby_amenities(args: Dict[str, Any]) -> Dict[str, Any]:
    call_id = log_tool_call("search_nearby_amenities", args)
    zip_code = (args.get("zip_code") or "").strip()
    category = (args.get("category") or "").strip()
    radius = int(args.get("radius_meters") or 2000)

    normalized = _normalize_category(category)
    canonical = _CATEGORY_ALIASES.get(normalized) or normalized
    place_type = CATEGORY_TO_PLACE_TYPE.get(canonical)
    if not place_type:
        try:
            geo = await geocode_zipcode(zip_code)
            result = await fetch_overpass_amenities(
                geo["latitude"], geo["longitude"], canonical, radius_meters=radius
            )
            result["note"] = (
                f"'{canonical}' not supported by Google Places. "
                "Counts from OpenStreetMap (no ratings)."
            )
            log_tool_result("search_nearby_amenities", "ok", call_id=call_id)
            return tool_json(result)
        except Exception as exc:
            log_tool_result("search_nearby_amenities", "error", call_id=call_id)
            return tool_error(
                "unsupported_category",
                {
                    "tool": "search_nearby_amenities",
                    "category": category,
                    "supported": sorted(CATEGORY_TO_PLACE_TYPE.keys()),
                    "message": str(exc),
                },
            )

    api_key = (args.get("api_key") or "").strip() or None
    api_key = api_key or os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        try:
            geo = await geocode_zipcode(zip_code)
            result = await fetch_overpass_amenities(
                geo["latitude"], geo["longitude"], canonical, radius_meters=radius
            )
            result["note"] = (
                "Google Places key missing. Using OpenStreetMap counts (no ratings)."
            )
            log_tool_result("search_nearby_amenities", "ok", call_id=call_id)
            return tool_json(result)
        except Exception as exc:
            log_tool_result("search_nearby_amenities", "error", call_id=call_id)
            return tool_error(
                "missing_api_key",
                {
                    "tool": "search_nearby_amenities",
                    "env": "GOOGLE_PLACES_API_KEY",
                    "message": str(exc),
                },
            )

    try:
        geo = await geocode_zipcode(zip_code)
    except Exception as exc:  # pragma: no cover - best-effort tool guard
        log_tool_result("search_nearby_amenities", "error", call_id=call_id)
        return tool_error(
            "geocode_failed",
            {"tool": "search_nearby_amenities", "message": str(exc)},
        )

    url = "https://places.googleapis.com/v1/places:searchNearby"
    payload = {
        "includedTypes": [place_type],
        "maxResultCount": 20,
        "locationRestriction": {
            "circle": {
                "center": {
                    "latitude": geo["latitude"],
                    "longitude": geo["longitude"],
                },
                "radius": radius,
            }
        },
    }
    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.displayName,places.rating,places.userRatingCount,places.formattedAddress"
        ),
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json=payload, headers=headers)

    if response.status_code != 200:
        log_tool_result("search_nearby_amenities", "error", call_id=call_id)
        return tool_error(
            "places_request_failed",
            {
                "tool": "search_nearby_amenities",
                "status": response.status_code,
                "error_type": _classify_http_error(response.status_code),
                "body": response.text[:500],
            },
        )

    data = response.json()
    places: List[Dict[str, Any]] = data.get("places") or []

    def _place_name(place: Dict[str, Any]) -> str:
        display = place.get("displayName") or {}
        return display.get("text") or ""

    def _place_rating(place: Dict[str, Any]) -> float | None:
        rating = place.get("rating")
        return float(rating) if rating is not None else None

    ratings = [r for r in (_place_rating(p) for p in places) if r is not None]
    avg_rating = statistics.mean(ratings) if ratings else None

    sorted_places = sorted(
        places,
        key=lambda p: (
            p.get("rating") or 0,
            p.get("userRatingCount") or 0,
        ),
        reverse=True,
    )

    top_rated = []
    for place in sorted_places[:5]:
        top_rated.append(
            {
                "name": _place_name(place),
                "rating": place.get("rating"),
                "user_ratings_total": place.get("userRatingCount"),
                "vicinity": place.get("formattedAddress"),
            }
        )

    result = {
        "category": canonical,
        "count": len(places),
        "top_rated": top_rated,
        "avg_rating": avg_rating,
        "center": {
            "latitude": geo["latitude"],
            "longitude": geo["longitude"],
            "city": geo.get("city"),
            "state": geo.get("state"),
        },
    }

    log_tool_result("search_nearby_amenities", "ok", call_id=call_id)
    return tool_json(result)
