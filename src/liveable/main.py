from __future__ import annotations

import asyncio
import json
import os
import re
import readline  # noqa: F401 - enables history navigation
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from lmnr import Laminar, observe
from rich.console import Console
from rich.text import Text

from . import __version__
from .agent import run_chat_query, trace_complete
from .ui import LiveableUI
from .tools.census import fetch_census_demographics
from .tools.derived_data import fetch_housing_prices
from .tools.web_crime import fetch_web_crime
from .tools.geo_profile import resolve_geo_profile
from .tools.walkscore import fetch_walkscore
from .tools.shared import ToolFailure, pop_tool_errors, set_log_sink, tool_error

ZIP_RE = re.compile(r"\b\d{5}\b")

REQUIRED_ENV = [
    "ANTHROPIC_API_KEY",
    "LMNR_PROJECT_API_KEY",
    "GOOGLE_PLACES_API_KEY",
    "CENSUS_API_KEY",
    "WALKSCORE_API_KEY",
]


@dataclass
class LocationContext:
    zip_code: str
    label: str
    geo: Dict[str, Any]
    census: Dict[str, Any] | None = None
    walkscore: Dict[str, Any] | None = None
    housing: Dict[str, Any] | None = None
    crime: Dict[str, Any] | None = None
    amenities: Dict[str, Any] = field(default_factory=dict)


def _validate_env(console: Console) -> None:
    missing = [key for key in REQUIRED_ENV if not os.getenv(key)]
    if missing:
        console.print(
            "[bold red]Missing required environment variables:[/bold red] "
            + ", ".join(missing)
        )
        raise SystemExit(1)


def _init_laminar() -> None:
    Laminar.initialize(project_api_key=os.environ["LMNR_PROJECT_API_KEY"])


def _summarize_contexts(
    contexts: Dict[str, LocationContext],
    current_zip: Optional[str],
) -> str:
    if current_zip:
        lines = [f"Active location: {current_zip}"]
    else:
        lines = ["Active location: none (set a ZIP to load a location)"]
    for zip_code, ctx in contexts.items():
        city = ctx.geo.get("city")
        state = ctx.geo.get("state")
        line = f"- {zip_code}: {city}, {state}"
        if ctx.walkscore and ctx.walkscore.get("walkscore") is not None:
            line += f" | Walk Score {ctx.walkscore.get('walkscore')}"
        if ctx.census and ctx.census.get("median_household_income"):
            line += f" | Income ${ctx.census.get('median_household_income'):,}"
        lines.append(line)
    return "\n".join(lines)


async def _load_location(zip_code: str) -> LocationContext:
    try:
        geo_payload = await resolve_geo_profile(zip_code)
    except Exception as exc:
        tool_error("geo_profile_failed", {"tool": "get_geo_profile", "message": str(exc)})
        raise ValueError("Unable to geocode location.") from exc

    try:
        census_payload = await fetch_census_demographics(zip_code)
    except ToolFailure as exc:
        tool_error(exc.message, exc.details)
        census_payload = None
    except Exception as exc:
        tool_error("census_request_failed", {"tool": "get_census_demographics", "message": str(exc)})
        census_payload = None

    address = f"ZIP {zip_code} {geo_payload.get('city')} {geo_payload.get('state')}"
    try:
        walk_payload = await fetch_walkscore(
            geo_payload.get("latitude"),
            geo_payload.get("longitude"),
            address,
        )
    except ToolFailure as exc:
        tool_error(exc.message, exc.details)
        walk_payload = None
    except Exception as exc:
        tool_error("walkscore_request_failed", {"tool": "get_walkscore", "message": str(exc)})
        walk_payload = None

    label = f"{geo_payload.get('city')}, {geo_payload.get('state')}"
    return LocationContext(
        zip_code=zip_code,
        label=label,
        geo=geo_payload,
        census=census_payload if census_payload else None,
        walkscore=walk_payload if walk_payload else None,
    )


@observe(name="user_query")
async def _answer_query(
    query: str,
    session_id: str,
    history: List[tuple[str, str]],
    context: str,
    user_id: str,
    current_zip: Optional[str],
    stream_callback,
) -> str:
    result = await run_chat_query(
        query,
        history=history,
        user_id=user_id,
        session_id=session_id,
        context=context,
        metadata={"current_zip": current_zip or ""},
        stream_callback=stream_callback,
    )
    trace_complete(mode="chat", status="ok")
    return result.report_markdown


@observe(name="session")
def _session_marker(session_id: str, location: str, user_id: str) -> Dict[str, str]:
    Laminar.set_trace_session_id(session_id=session_id)
    Laminar.set_trace_user_id(user_id=user_id)
    return {"session_id": session_id, "location": location, "user_id": user_id}


def _render_compare(
    ui: LiveableUI,
    contexts: Dict[str, LocationContext],
    zip_a: str,
    zip_b: str,
) -> None:
    a = contexts.get(zip_a)
    b = contexts.get(zip_b)
    if not a or not b:
        ui.console.print("Missing one of the locations. Load it first.")
        return

    async def _ensure_data(ctx: LocationContext) -> None:
        if ctx.housing is None:
            try:
                ctx.housing = await fetch_housing_prices(ctx.zip_code)
            except ToolFailure as exc:
                tool_error("housing_data_unavailable", {"tool": "search_housing_prices", "details": exc.details})
            except Exception as exc:
                tool_error("housing_data_unavailable", {"tool": "search_housing_prices", "message": str(exc)})
        if ctx.crime is None:
            try:
                ctx.crime = await fetch_web_crime(ctx.zip_code)
            except ToolFailure as exc:
                tool_error("crime_data_unavailable", {"tool": "search_crime_safety", "details": exc.details})
            except Exception as exc:
                tool_error("crime_data_unavailable", {"tool": "search_crime_safety", "message": str(exc)})

    asyncio.run(_ensure_data(a))
    asyncio.run(_ensure_data(b))

    def _rent(ctx: LocationContext) -> Optional[Any]:
        if ctx.housing:
            return ctx.housing.get("median_rent")
        if ctx.census:
            return ctx.census.get("median_rent")
        return None

    def _income(ctx: LocationContext) -> Optional[Any]:
        return ctx.census.get("median_household_income") if ctx.census else None

    def _rate(ctx: LocationContext, key: str) -> Optional[Any]:
        if ctx.crime:
            return ctx.crime.get(key)
        return None

    rows = {
        "Median Rent": (str(_rent(a) or "n/a"), str(_rent(b) or "n/a")),
        "Median Income": (str(_income(a) or "n/a"), str(_income(b) or "n/a")),
        "Walk Score": (
            str(a.walkscore.get("walkscore") if a.walkscore else "n/a"),
            str(b.walkscore.get("walkscore") if b.walkscore else "n/a"),
        ),
        "Transit Score": (
            str(a.walkscore.get("transit_score") if a.walkscore else "n/a"),
            str(b.walkscore.get("transit_score") if b.walkscore else "n/a"),
        ),
        "Violent Crime (per 100k)": (
            str(_rate(a, "violent_crime_rate") or "n/a"),
            str(_rate(b, "violent_crime_rate") or "n/a"),
        ),
        "Property Crime (per 100k)": (
            str(_rate(a, "property_crime_rate") or "n/a"),
            str(_rate(b, "property_crime_rate") or "n/a"),
        ),
        "Homeownership %": (
            str(a.census.get("pct_owner_occupied") if a.census else "n/a"),
            str(b.census.get("pct_owner_occupied") if b.census else "n/a"),
        ),
    }
    ui.render_compare_table(f"{a.label} ({a.zip_code})", f"{b.label} ({b.zip_code})", rows)


def main() -> None:
    load_dotenv()
    os.environ.setdefault("LIVEABLE_VERBOSE", "1")
    console = Console()
    ui = LiveableUI(console)
    set_log_sink(ui.handle_event)

    ui.render_header(__version__)

    _validate_env(console)
    _init_laminar()

    session_id = os.getenv("LIVEABLE_SESSION_ID") or os.urandom(8).hex()
    user_id = os.getenv("LIVEABLE_USER_ID") or os.getenv("USER") or "cli-user"

    _session_marker(session_id, "unset", user_id)

    contexts: Dict[str, LocationContext] = {}
    current_zip: Optional[str] = None

    console.print(
        Text(
            "Ask a question, or set a location with /set 20001",
            style="dim",
        )
    )

    history: List[tuple[str, str]] = []

    while True:
        context_line = ui.render_context_line(contexts, current_zip)
        ui.maybe_print_context(context_line)
        try:
            query = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not query:
            continue
        if query.lower() in {"exit", "quit", "q"}:
            break
        if query == "/help":
            ui.render_help()
            continue
        if query == "/locations":
            ui.render_locations(contexts)
            continue
        if query == "/clear":
            contexts.clear()
            history.clear()
            current_zip = None
            ui.console.print("Session cleared.")
            continue

        if query.startswith("/set"):
            zip_match = ZIP_RE.search(query)
            if not zip_match:
                console.print("Usage: /set 5-digit-zip")
                continue
            zip_code = zip_match.group(0)
            if zip_code not in contexts:
                try:
                    pop_tool_errors()
                    contexts[zip_code] = asyncio.run(_load_location(zip_code))
                except Exception as exc:
                    console.print(f"Failed to load location: {exc}")
                    continue
            current_zip = zip_code
            context_line = ui.render_context_line(contexts, current_zip)
            ui.maybe_print_context(context_line)
            continue

        if query.startswith("/compare"):
            parts = query.split()
            if len(parts) < 3:
                console.print("Usage: /compare ZIP1 ZIP2")
                continue
            zip_a = parts[1]
            zip_b = parts[2]
            if zip_a not in contexts:
                contexts[zip_a] = asyncio.run(_load_location(zip_a))
            if zip_b not in contexts:
                contexts[zip_b] = asyncio.run(_load_location(zip_b))
            _render_compare(ui, contexts, zip_a, zip_b)
            continue

        # Load any ZIPs referenced in the query.
        for match in ZIP_RE.findall(query):
            if match not in contexts:
                try:
                    contexts[match] = asyncio.run(_load_location(match))
                except Exception as exc:
                    console.print(f"Failed to load location {match}: {exc}")
                    continue
            current_zip = match

        context_text = _summarize_contexts(contexts, current_zip)
        pop_tool_errors()

        ui.begin_answer()
        response = asyncio.run(
            _answer_query(
                query,
                session_id=session_id,
                history=history,
                context=context_text,
                user_id=user_id,
                current_zip=current_zip,
                stream_callback=ui.stream_answer,
            )
        )
        ui.end_answer()

        tool_errors = pop_tool_errors()
        if tool_errors:
            console.print(f":warning: {len(tool_errors)} tool failures recorded.")
            console.print("Tool Failures (System):")
            console.print(
                "```json\n"
                + json.dumps(tool_errors, ensure_ascii=True, indent=2)
                + "\n```"
            )

        history.append((query, response))

    console.print("\nBye!")


if __name__ == "__main__":
    main()
