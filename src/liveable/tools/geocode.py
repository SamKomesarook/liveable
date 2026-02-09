from __future__ import annotations

from typing import Any, Dict

from claude_agent_sdk import tool
from lmnr import observe

from .shared import geocode_zipcode, log_tool_call, log_tool_result, tool_error, tool_json


@tool(
    "geocode_zip",
    "Geocode a US ZIP code to city/state and coordinates.",
    {"zip_code": str},
)
@observe()
async def geocode_zip(args: Dict[str, Any]) -> Dict[str, Any]:
    call_id = log_tool_call("geocode_zip", args)
    zip_code = (args.get("zip_code") or "").strip()
    try:
        data = await geocode_zipcode(zip_code)
    except Exception as exc:  # pragma: no cover - best-effort tool guard
        log_tool_result("geocode_zip", "error", call_id=call_id)
        return tool_error("geocode_failed", {"tool": "geocode_zip", "message": str(exc)})
    log_tool_result("geocode_zip", "ok", call_id=call_id)
    return tool_json(data)
