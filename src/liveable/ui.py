from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


TOOL_DISPLAY_NAMES = {
    "geocode_zip": "Geocoding",
    "get_geo_profile": "Geocoding",
    "search_nearby_amenities": "Amenity Search",
    "get_census_demographics": "Census Data",
    "get_walkscore": "Walk Score",
    "get_hud_fmr": "HUD FMR",
    "search_housing_prices": "Housing Prices",
    "search_new_developments": "Development Search",
    "search_crime_safety": "Crime & Safety Search",
    "search_noise_proxies": "Noise Proxy Search",
    "search_osm_amenities": "OSM Amenity Search",
    "search_overpass_amenities": "Overpass Amenity Search",
}

TOOL_RESULT_SUMMARIES = {
    "geocode_zip": "Resolved",
    "get_geo_profile": "Resolved",
    "search_nearby_amenities": "Fetched amenities",
    "get_census_demographics": "Loaded demographics",
    "get_walkscore": "Loaded scores",
    "get_hud_fmr": "Loaded rents",
    "search_housing_prices": "Loaded rent benchmarks",
    "search_new_developments": "Loaded permit records",
    "search_crime_safety": "Loaded crime rates",
    "search_noise_proxies": "Loaded noise proxies",
    "search_osm_amenities": "Loaded amenity counts",
    "search_overpass_amenities": "Loaded amenity counts",
}

ASCII_LOGO = (
    "██      ██ ██    ██ ██████  ██████  ██████  ██      ██████\n"
    "██      ██ ██    ██ ██      ██  ██  ██  ██  ██      ██\n"
    "██      ██  ██  ██  ████    ██████  ██████  ██      ████\n"
    "██      ██   ████   ██      ██  ██  ██  ██  ██      ██\n"
    "██████  ██    ██    ██████  ██  ██  ██████  ██████  ██████"
)


@dataclass
class ToolRun:
    name: str
    preview: str
    started: float
    status: Optional[str] = None


class LiveableUI:
    def __init__(self, console: Console) -> None:
        self.console = console
        self._tool_runs: Dict[int, ToolRun] = {}
        self._active_tools: Dict[int, str] = {}
        self._answer_line_start = True
        self._last_context_line: Optional[str] = None

    def render_header(self, version: str) -> None:
        header = Panel(
            Text(f" liveable {version} ", style="bold"),
            border_style="bright_cyan",
            expand=True,
        )
        self.console.print(header)
        self.console.print(Text(ASCII_LOGO, style="#002FA7"))
        self.console.print(Text("Your AI neighborhood analyst.", style="dim"))
        self.console.print(Text("Powered by Claude Agent SDK · Traced by Laminar", style="dim"))
        self.console.print()

    def render_context_line(
        self,
        contexts: Dict[str, Any],
        current_zip: Optional[str],
    ) -> Optional[str]:
        if current_zip and current_zip in contexts:
            ctx = contexts[current_zip]
            city = ctx.geo.get("city")
            state = ctx.geo.get("state")
            pop = ctx.census.get("population") if ctx.census else None
            walk = ctx.walkscore.get("walkscore") if ctx.walkscore else None
            parts = []
            if pop:
                parts.append(f"Pop: {pop:,}")
            if walk is not None:
                parts.append(f"Walk Score: {walk}")
            suffix = " · ".join(parts) if parts else "Basics unavailable"
            return f"📍 {city}, {state} ({current_zip}) · {suffix}"

        if contexts:
            items = []
            for zip_code, ctx in contexts.items():
                state = ctx.geo.get("state")
                items.append(f"{state} {zip_code}")
            return "📍 " + " · ".join(items)

        return None

    def maybe_print_context(self, line: Optional[str]) -> None:
        if not line:
            return
        if line == self._last_context_line:
            return
        self.console.print(line)
        self._last_context_line = line

    def render_help(self) -> None:
        self.console.print(
            Text(
                "Commands: /set ZIP  /compare ZIP1 ZIP2  /locations  /clear  /help  /exit",
                style="bright_cyan",
            )
        )

    def render_locations(self, contexts: Dict[str, Any]) -> None:
        if not contexts:
            self.console.print("No locations loaded.")
            return
        for zip_code, ctx in contexts.items():
            city = ctx.geo.get("city")
            state = ctx.geo.get("state")
            self.console.print(f"- {zip_code}: {city}, {state}")

    def render_compare_table(
        self,
        a_label: str,
        b_label: str,
        rows: Dict[str, tuple[str, str]],
    ) -> None:
        table = Table(show_lines=True)
        table.add_column("")
        table.add_column(a_label)
        table.add_column(b_label)
        for label, (a_val, b_val) in rows.items():
            table.add_row(label, a_val, b_val)
        self.console.print(table)

    def handle_event(self, event: Any) -> None:
        if isinstance(event, str):
            self.console.print(event, style="dim")
            return

        if not isinstance(event, dict):
            return

        event_type = event.get("type")
        if event_type == "tool_start":
            call_id = int(event.get("call_id", 0))
            name = event.get("name") or "tool"
            preview = event.get("preview") or ""
            display = TOOL_DISPLAY_NAMES.get(name, name)
            self._active_tools[call_id] = display
            self._tool_runs[call_id] = ToolRun(
                name=name,
                preview=preview,
                started=event.get("ts", time.perf_counter()),
            )
            if not self._answer_line_start:
                self.console.print()
                self._answer_line_start = True
            self.console.print(
                f"  ● {display}(\"{preview}\")",
                style="bright_cyan",
            )
            self._render_active_tools()
            return

        if event_type == "tool_end":
            call_id = int(event.get("call_id", 0))
            run = self._tool_runs.get(call_id)
            self._active_tools.pop(call_id, None)
            if not run:
                return
            run.status = event.get("status")
            if run.status == "error":
                return
            elapsed = event.get("elapsed")
            if elapsed is None:
                elapsed = time.perf_counter() - run.started
            summary = TOOL_RESULT_SUMMARIES.get(run.name, "Completed")
            self.console.print(
                f"    └ {summary} in {elapsed:.1f}s",
                style="dim",
            )
            self._render_active_tools()
            return

        if event_type == "tool_error":
            call_id = int(event.get("call_id", 0))
            run = self._tool_runs.get(call_id)
            self._active_tools.pop(call_id, None)
            elapsed = event.get("elapsed")
            if elapsed is None and run:
                elapsed = time.perf_counter() - run.started
            reason = event.get("message") or event.get("error") or "Unknown error"
            line = f"└ ✗ Failed: {reason}"
            if elapsed is not None:
                line += f" ({elapsed:.1f}s)"
            self.console.print(f"    {line}", style="red")
            self._render_active_tools()
            return

    def begin_answer(self) -> None:
        self._answer_line_start = True

    def stream_answer(self, text: str) -> None:
        if not text:
            return
        prefix = "  " if self._answer_line_start else ""
        body = text.replace("\n", "\n  ")
        self.console.print(prefix + body, end="", soft_wrap=True, highlight=False)
        self._answer_line_start = text.endswith("\n")

    def end_answer(self) -> None:
        self.console.print()
        self._answer_line_start = True

    def _render_active_tools(self) -> None:
        if len(self._active_tools) < 2:
            return
        active = ", ".join(self._active_tools.values())
        self.console.print(f"    ⏳ Active: {active}", style="dim")
