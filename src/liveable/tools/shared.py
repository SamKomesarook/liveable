from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

import httpx


ZIP_RE = re.compile(r"^\d{5}$")
_TOOL_ERRORS: List[Dict[str, Any]] = []
_LOG_SINK = None
_TOOL_CALL_COUNT = 0
_TOOL_CALL_ID = 0
_LAST_TOOL_CALL_ID: Optional[int] = None


class ToolFailure(Exception):
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


def tool_json(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=True, sort_keys=True),
            }
        ]
    }


def _is_verbose() -> bool:
    return os.getenv("LIVEABLE_VERBOSE", "").lower() in {"1", "true", "yes", "on"}


def _short_json(payload: Dict[str, Any], limit: int = 180) -> str:
    try:
        text = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    except TypeError:
        text = str(payload)
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _preview_from_payload(payload: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in ("zip_code", "city", "address", "category"):
        value = payload.get(key)
        if value:
            parts.append(str(value))
    if parts:
        return " ".join(parts)
    return _short_json(payload)


def log_step(message: str) -> None:
    if not _is_verbose():
        return
    if _LOG_SINK:
        _LOG_SINK(message)
        return
    print(message, file=sys.stderr, flush=True)


def set_log_sink(sink) -> None:
    global _LOG_SINK
    _LOG_SINK = sink


def reset_tool_counter() -> None:
    global _TOOL_CALL_COUNT
    _TOOL_CALL_COUNT = 0


def get_tool_counter() -> int:
    return _TOOL_CALL_COUNT


def log_tool_call(name: str, payload: Dict[str, Any]) -> int:
    global _TOOL_CALL_COUNT, _TOOL_CALL_ID, _LAST_TOOL_CALL_ID
    _TOOL_CALL_COUNT += 1
    _TOOL_CALL_ID += 1
    _LAST_TOOL_CALL_ID = _TOOL_CALL_ID
    preview = _preview_from_payload(payload)
    event = {
        "type": "tool_start",
        "name": name,
        "preview": preview,
        "call_id": _TOOL_CALL_ID,
        "ts": time.perf_counter(),
    }
    if _is_verbose() and _LOG_SINK:
        _LOG_SINK(event)
    else:
        log_step(f"- {name} {preview}")
    return _TOOL_CALL_ID


def log_tool_result(name: str, status: str, call_id: Optional[int] = None) -> None:
    call_id = call_id or _LAST_TOOL_CALL_ID
    event = {
        "type": "tool_end",
        "name": name,
        "status": status,
        "call_id": call_id,
        "ts": time.perf_counter(),
    }
    if _is_verbose() and _LOG_SINK:
        _LOG_SINK(event)
    else:
        log_step(f"  -> {name}: {status}")


def record_tool_error(payload: Dict[str, Any]) -> None:
    _TOOL_ERRORS.append(payload)


def pop_tool_errors() -> List[Dict[str, Any]]:
    errors = list(_TOOL_ERRORS)
    _TOOL_ERRORS.clear()
    return errors


def tool_error(message: str, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"status": "error", "error": message}
    if details:
        payload["details"] = details
    if _is_verbose() and _LOG_SINK:
        _LOG_SINK(
            {
                "type": "tool_error",
                "message": message,
                "error": message,
                "details": details or {},
                "call_id": _LAST_TOOL_CALL_ID,
                "ts": time.perf_counter(),
            }
        )
    else:
        log_step(f"  -> tool error: {message}")
    record_tool_error(payload)
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=True, sort_keys=True),
            }
        ],
        "is_error": True,
    }


def parse_json_loose(text: str) -> Dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("Empty response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Best-effort extraction for model outputs that wrap JSON in prose.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


async def geocode_zipcode(zip_code: str) -> Dict[str, Any]:
    if not ZIP_RE.match(zip_code):
        raise ValueError("zip_code must be a 5-digit string")

    url = f"https://api.zippopotam.us/us/{zip_code}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url)

    if response.status_code != 200:
        raise ValueError(f"Geocoding failed with status {response.status_code}")

    data = response.json()
    places = data.get("places") or []
    if not places:
        raise ValueError("No places found for zip code")

    place = places[0]
    return {
        "zip_code": zip_code,
        "city": place.get("place name"),
        "state": place.get("state abbreviation"),
        "latitude": float(place.get("latitude")),
        "longitude": float(place.get("longitude")),
    }
