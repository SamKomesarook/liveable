from __future__ import annotations

import os
import asyncio
from typing import Any, Dict, Optional, Tuple

import httpx
from claude_agent_sdk import tool
from lmnr import observe

from .shared import ToolFailure, geocode_zipcode, log_tool_call, log_tool_result, tool_error, tool_json


OVERPASS_URL = os.environ.get("OVERPASS_URL", "https://overpass.private.coffee/api/interpreter")


PROXY_TAGS: Dict[str, Tuple[str, str, bool]] = {
    "street_lights": ("highway", "street_lamp", False),
    "surveillance": ("man_made", "surveillance", False),
    "benches": ("amenity", "bench", False),
    "post_boxes": ("amenity", "post_box", False),
    "airports": ("aeroway", "aerodrome|runway", True),
    "rail": ("railway", "rail", False),
    "major_roads": ("highway", "motorway|trunk|primary", True),
}

_CACHE: Dict[Tuple[str, int], Dict[str, Any]] = {}


def _build_count_query(
    lat: float,
    lon: float,
    radius: int,
    key: str,
    value: str,
    regex: bool,
) -> str:
    op = "~" if regex else "="
    return (
        "[out:json][timeout:10];"
        f"(node[{key}{op}\"{value}\"](around:{radius},{lat},{lon});"
        f"way[{key}{op}\"{value}\"](around:{radius},{lat},{lon});"
        f"relation[{key}{op}\"{value}\"](around:{radius},{lat},{lon}););"
        "out count;"
    )


def _parse_count(data: Dict[str, Any]) -> Optional[int]:
    elements = data.get("elements")
    if not isinstance(elements, list) or not elements:
        return 0
    tags = elements[0].get("tags") if isinstance(elements[0], dict) else {}
    if not isinstance(tags, dict):
        return 0
    total = tags.get("total")
    try:
        return int(total)
    except (TypeError, ValueError):
        return 0


async def _query_overpass_count(query: str, max_retries: int = 1, timeout: float = 12.0) -> Optional[int]:
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(OVERPASS_URL, params={"data": query})
            if response.status_code == 200:
                return _parse_count(response.json())
            if response.status_code in {429, 504} and attempt < max_retries:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            return None
        except (httpx.TimeoutException, httpx.ConnectError):
            if attempt < max_retries:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            return None
    return None


async def fetch_noise_proxies(zip_code: str, radius_meters: int = 500) -> Dict[str, Any]:
    cache_key = (zip_code, radius_meters)
    if cache_key in _CACHE:
        return _CACHE[cache_key]
    geo = await geocode_zipcode(zip_code)
    lat = geo["latitude"]
    lon = geo["longitude"]
    counts: Dict[str, Optional[int]] = {}
    semaphore = asyncio.Semaphore(3)

    async def _run(name: str, key: str, value: str, regex: bool) -> None:
        async with semaphore:
            query = _build_count_query(lat, lon, radius_meters, key, value, regex)
            counts[name] = await _query_overpass_count(query, max_retries=1, timeout=12.0)

    tasks = [
        _run(name, key, value, regex) for name, (key, value, regex) in PROXY_TAGS.items()
    ]
    await asyncio.gather(*tasks)
    errors = [name for name, value in counts.items() if value is None]

    result = {
        "zip_code": zip_code,
        "radius_meters": radius_meters,
        "center": {"latitude": lat, "longitude": lon},
        "proxy_counts": counts,
        "errors": errors if errors else None,
        "source": OVERPASS_URL,
        "note": "Proxy counts for neighborhood infrastructure and transport noise signals.",
    }
    _CACHE[cache_key] = result
    return result


@tool(
    "search_noise_proxies",
    "Assess noise risk proxies (airports, rail, major roads) using Overpass API.",
    {"zip_code": str, "radius_meters": int},
)
@observe()
async def search_noise_proxies(args: Dict[str, Any]) -> Dict[str, Any]:
    call_id = log_tool_call("search_noise_proxies", args)
    zip_code = (args.get("zip_code") or "").strip()
    radius = int(args.get("radius_meters") or 500)

    try:
        result = await fetch_noise_proxies(zip_code, radius_meters=radius)
    except ToolFailure as exc:
        log_tool_result("search_noise_proxies", "error", call_id=call_id)
        return tool_error(exc.message, exc.details)
    except Exception as exc:  # pragma: no cover
        log_tool_result("search_noise_proxies", "error", call_id=call_id)
        return tool_error(
            "overpass_request_failed",
            {"tool": "search_noise_proxies", "message": str(exc)},
        )

    log_tool_result("search_noise_proxies", "ok", call_id=call_id)
    return tool_json(result)
