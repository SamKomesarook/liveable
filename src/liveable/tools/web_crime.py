from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional

from browser_use_sdk import BrowserUse
from claude_agent_sdk import tool
from lmnr import observe

from .shared import (
    ToolFailure,
    geocode_zipcode,
    log_tool_call,
    log_tool_result,
    parse_json_loose,
    tool_error,
    tool_json,
)


def _require_api_key(api_key: Optional[str]) -> str:
    api_key = api_key or os.environ.get("BROWSER_USE_API_KEY")
    if not api_key:
        raise ToolFailure(
            "missing_api_key",
            {"tool": "search_crime_safety", "env": "BROWSER_USE_API_KEY"},
        )
    return api_key


_CACHE: Dict[str, Dict[str, Any]] = {}


def _run_browser_task(task: str, api_key: str) -> str:
    client = BrowserUse(api_key=api_key)
    task_obj = client.tasks.create_task(task=task, llm="browser-use-llm")
    result = task_obj.complete()
    return result.output or ""


async def fetch_web_crime(zip_code: str) -> Dict[str, Any]:
    if zip_code in _CACHE:
        return _CACHE[zip_code]
    api_key = _require_api_key(None)
    geo = await geocode_zipcode(zip_code)
    city = geo.get("city")
    state = geo.get("state")

    tasks = [
        f"""
You are collecting crime statistics for {city}, {state}. Follow these steps:
1) Search for "{city} {state} crime rate per capita" and "{city} {state} crime statistics".
2) Prefer official city/county dashboards and annual reports.
3) Secondary sources: AreaVibes, Macrotrends, reputable local news citing official stats.
4) Avoid: crimegrade.org, bestplaces.net, neighborhoodscout.com.

Return a JSON object ONLY with this schema:
{{
  "violent_crime_rate": number | null,
  "property_crime_rate": number | null,
  "rate_unit": "per 1,000" | "per 100,000" | null,
  "comparison_to_national": string | null,
  "year": number | null,
  "sources": [string],
  "notes": string
}}

If data is only city-level, note that in "notes".
""",
        f"""
Find the most recent {city}, {state} crime rate per capita.
Return JSON ONLY with:
{{
  "violent_crime_rate": number | null,
  "property_crime_rate": number | null,
  "rate_unit": "per 1,000" | "per 100,000" | null,
  "year": number | null,
  "sources": [string],
  "notes": string
}}
""",
    ]

    last_error: str | None = None
    for index, task in enumerate(tasks):
        try:
            output = await asyncio.wait_for(
                asyncio.to_thread(_run_browser_task, task, api_key),
                timeout=45.0 if index == 0 else 35.0,
            )
        except asyncio.TimeoutError:
            last_error = "Browser Use timed out"
            continue
        if not output:
            last_error = "Empty output from Browser Use"
            continue
        try:
            data = parse_json_loose(output)
        except Exception:
            data = None
        if not isinstance(data, dict):
            result = {
                "zip_code": zip_code,
                "city": city,
                "state": state,
                "violent_crime_rate": None,
                "property_crime_rate": None,
                "rate_unit": None,
                "comparison_to_national": None,
                "year": None,
                "sources": [],
                "note": "Browser Use returned unstructured output; unable to parse crime rates.",
                "raw_output": output[:2000],
            }
            _CACHE[zip_code] = result
            return result
        result = {
            "zip_code": zip_code,
            "city": city,
            "state": state,
            "violent_crime_rate": data.get("violent_crime_rate"),
            "property_crime_rate": data.get("property_crime_rate"),
            "rate_unit": data.get("rate_unit"),
            "comparison_to_national": data.get("comparison_to_national"),
            "year": data.get("year"),
            "sources": data.get("sources") or [],
            "note": data.get("notes"),
        }
        _CACHE[zip_code] = result
        return result

    raise ToolFailure(
        "crime_search_timeout",
        {"tool": "search_crime_safety", "message": last_error or "No results"},
    )


@tool(
    "search_crime_safety",
    "Search for crime statistics via a web search API and return structured summaries.",
    {"zip_code": str},
)
@observe()
async def search_crime_safety(args: Dict[str, Any]) -> Dict[str, Any]:
    call_id = log_tool_call("search_crime_safety", args)
    zip_code = (args.get("zip_code") or "").strip()

    try:
        result = await fetch_web_crime(zip_code)
    except ToolFailure as exc:
        log_tool_result("search_crime_safety", "error", call_id=call_id)
        return tool_error(exc.message, exc.details)
    except Exception as exc:  # pragma: no cover
        log_tool_result("search_crime_safety", "error", call_id=call_id)
        return tool_error(
            "crime_search_failed",
            {"tool": "search_crime_safety", "message": str(exc)},
        )

    log_tool_result("search_crime_safety", "ok", call_id=call_id)
    return tool_json(result)
