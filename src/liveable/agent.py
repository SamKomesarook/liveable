from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Sequence, Tuple

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    TextBlock,
    create_sdk_mcp_server,
)
from lmnr import Laminar, observe

from .tools import (
    get_geo_profile,
    get_hud_fmr,
    get_rentcast_market,
    get_rentcast_sale_listings,
    search_noise_proxies,
    search_overpass_amenities,
    search_osm_amenities,
    geocode_zip,
    get_census_demographics,
    get_walkscore,
    search_crime_safety,
    search_housing_prices,
    search_nearby_amenities,
    search_new_developments,
)
from .tools.shared import pop_tool_errors

SYSTEM_PROMPT = """
You are a neighborhood analysis assistant. You help people understand neighborhoods by answering their specific questions with real data.

When a user provides a location:
1. Geocode it to get coordinates, city, state, and zip.
2. Pull basic census demographics and walkability scores.
3. Present a brief one-line summary and invite questions.
If the user provides a non-ZIP location, ask for a 5-digit ZIP code.
If there is no active location in the session context, ask the user to provide a 5-digit ZIP code before calling location-specific tools.
If the user asks for a general neighborhood analysis without preferences, ask 2-3 short questions about priorities (budget, property type, school level, commute, noise).

When answering questions:
- Use the appropriate tools to gather data on-demand. Don't gather everything upfront.
- Always cite your data source and confidence level.
- When data is unavailable or incomplete, say so explicitly.
- When making comparisons, use concrete numbers, not vague qualitative assessments.
- If the user references another location, load it and keep both in context.
- Remember what you've already looked up in this session; don't re-fetch data unnecessarily.
- When the user asks for value, overpaying, affordability, or "best place" guidance, ask 1-3 clarifying questions if key details are missing (budget, property type, beds/baths, timeline, commute needs, school level, noise sensitivity).
- End every response with: "Sources: ... · Confidence: ...".
- You can delegate to subagents for focused analysis: crime-analyst, schools-analyst, noise-analyst, housing-analyst. Use them to parallelize work and keep your own response concise.
- For amenities, use categories supported by search_nearby_amenities:
  restaurants, bars, nightlife, cafes, gyms, parks, grocery_stores, schools, hospitals,
  transit_stations, pharmacies, libraries, museums, shopping_malls, movie_theaters.
  Use Google Places for ratings/quality signals and search_osm_amenities for coverage counts.
  You can call both to cross-check density vs quality. If Google Places fails or a key is missing,
  fall back to search_osm_amenities (counts only).
  The search_nearby_amenities tool routes unsupported categories to Overpass automatically.
  You can pass categories like universities, fire_stations, or police and it will fall back to OSM.
  If you already have coordinates, you can call search_overpass_amenities directly.
  Normalize variants like primary_school/elementary_school/middle_school/high_school/secondary_school/kindergarten to "schools".
  You may adjust radius_meters based on the question: smaller (500-1000m) for "nearby/walkable",
  larger (1500-3000m) for "in the area" or broader coverage.
- Do not use external web browsing; only the provided tools are available.

You can answer questions about:
- Safety and crime rates
- Walkability, transit, and bikeability
- Dining, nightlife, bars, and restaurants
- Parks, gyms, and outdoor activities
- Schools and family amenities (use Google Places)
- Housing prices, rent, and affordability
- New construction and development pipeline
- Demographics and community character
- Grocery stores, shopping, and daily errands
- Noise risk via proximity to airports, rail, and major roads
- Comparisons between any locations discussed in the session

Use these tools as needed:
- search_housing_prices for rent benchmarks (HUD FMR-based)
- search_crime_safety for crime statistics via Browser Use web search (city/state level when ZIP data is unavailable)
- search_new_developments for permit pipelines (requires DEV_PERMITS_BASE_URL)
- search_noise_proxies for noise risk signals (Overpass API, no key)

When using search_crime_safety:
- Prefer official city/county crime dashboards and annual reports first.
- Secondary sources: AreaVibes, Macrotrends, reputable local news citing official stats.
- Avoid blocked or paywalled sites: crimegrade.org, bestplaces.net, neighborhoodscout.com.
- Extract and cite: violent crime rate, property crime rate, unit (per 1,000 or per 100,000),
  year of data, and comparison to national average when present.

When the user asks a comparative question or uses /compare:
- Present data side by side.
- Use actual numbers, not scores.
- Note where data confidence differs between locations.

Keep responses concise and data-driven. Don't pad answers with generic observations. If the data tells a clear story, state it plainly.
""".strip()

CHAT_SYSTEM_PROMPT = SYSTEM_PROMPT


@dataclass
class LiveableResult:
    report_markdown: str
    trace_id: str | None
    session_id: str


@observe()
def trace_complete(mode: str, status: str = "ok") -> Dict[str, str]:
    return {"mode": mode, "status": status}


def _build_mcp_server():
    return create_sdk_mcp_server(
        name="liveable",
        tools=[
            geocode_zip,
            get_geo_profile,
            search_nearby_amenities,
            get_census_demographics,
            get_walkscore,
            get_hud_fmr,
            get_rentcast_market,
            get_rentcast_sale_listings,
            search_noise_proxies,
            search_overpass_amenities,
            search_osm_amenities,
            search_housing_prices,
            search_new_developments,
            search_crime_safety,
        ],
    )


def _build_subagents(message: str | None = None) -> Dict[str, AgentDefinition]:
    agents: Dict[str, AgentDefinition] = {
        "general-purpose": AgentDefinition(
            description=(
                "General-purpose neighborhood analyst. Use for any task not covered by other subagents."
            ),
            prompt=(
                "You are a general-purpose neighborhood analysis assistant. "
                "Do not ask for files, code, or repositories. Use the available neighborhood tools "
                "to answer the user's question with sources and confidence."
            ),
        ),
        "crime-analyst": AgentDefinition(
            description="Crime and safety analyst. Use to gather crime stats and safety context.",
            prompt=(
                "You are a crime and safety analyst. Use available tools to retrieve crime data, "
                "note data scope (city/state/zip), and summarize with clear uncertainty."
            ),
            tools=[
                "mcp__liveable__search_crime_safety",
                "mcp__liveable__search_osm_amenities",
            ],
        ),
        "schools-analyst": AgentDefinition(
            description="Schools and education analyst. Use to assess school availability and education context.",
            prompt=(
                "You analyze schools and education access. Use Google Places for quality/ratings "
                "and OSM counts for coverage. Be explicit that Places is not a ratings agency."
            ),
            tools=[
                "mcp__liveable__search_nearby_amenities",
                "mcp__liveable__search_osm_amenities",
            ],
        ),
        "noise-analyst": AgentDefinition(
            description="Noise and environment analyst. Use to assess noise risk proxies.",
            prompt=(
                "You assess noise risk using proximity to airports, rail, and major roads. "
                "Use the noise proxy tool and summarize the nearest distances."
            ),
            tools=["mcp__liveable__search_noise_proxies"],
        ),
        "housing-analyst": AgentDefinition(
            description="Housing market analyst. Use to assess prices, rents, and value context.",
            prompt=(
                "You analyze housing market data for a ZIP. Use RentCast/HUD tools to estimate "
                "median prices and rents and highlight data limits."
            ),
            tools=[
                "mcp__liveable__search_housing_prices",
                "mcp__liveable__get_rentcast_market",
                "mcp__liveable__get_rentcast_sale_listings",
                "mcp__liveable__get_hud_fmr",
            ],
        ),
    }

    if message:
        agents["topic-analyst"] = AgentDefinition(
            description=(
                "Flexible specialist for any user-specific topic not covered by other analysts."
            ),
            prompt=(
                "You are a flexible specialist. Focus on the user's request and use any relevant tools "
                "to gather facts. Provide a short bullet summary with sources and confidence.\n\n"
                f"User request:\n{message}"
            ),
        )

    return agents


def _build_tool_guard(allowed_subagents: set[str]):
    async def can_use_tool(
        tool_name: str,
        tool_input: Dict[str, Any],
        _context,
    ):
        if tool_name != "Task":
            return PermissionResultAllow()
        subagent_type = (
            tool_input.get("subagent_type")
            or tool_input.get("subagentType")
            or tool_input.get("agent")
        )
        if subagent_type in allowed_subagents:
            return PermissionResultAllow()
        return PermissionResultDeny(
            message=(
                "Only these subagents are allowed: "
                f"{', '.join(sorted(allowed_subagents))}."
            )
        )

    return can_use_tool


async def _collect_report(
    client: ClaudeSDKClient,
    stream_callback: Callable[[str], None] | None = None,
) -> str:
    parts: List[str] = []
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
                    if stream_callback:
                        stream_callback(block.text)
    report = "".join(parts).strip()
    return report


def _build_chat_prompt(
    message: str,
    history: Sequence[Tuple[str, str]] | None = None,
    context: str | None = None,
) -> str:
    lines: List[str] = []
    if context:
        lines.append("Session context:")
        lines.append(context)
        lines.append("")

    if not history:
        lines.append(f"User: {message}")
        lines.append("Assistant:")
        return "\n".join(lines)

    lines.append("Conversation so far:")
    for user_msg, assistant_msg in history[-6:]:
        lines.append(f"User: {user_msg}")
        lines.append(f"Assistant: {assistant_msg}")
    lines.append(f"User: {message}")
    lines.append("Assistant:")
    return "\n".join(lines)


@observe()
async def run_chat_query(
    message: str,
    history: Sequence[Tuple[str, str]] | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    context: str | None = None,
    stream_callback: Callable[[str], None] | None = None,
    metadata: Dict[str, Any] | None = None,
) -> LiveableResult:
    pop_tool_errors()
    session_id = session_id or str(uuid.uuid4())
    Laminar.set_trace_session_id(session_id=session_id)
    Laminar.set_trace_user_id(user_id=user_id or "cli-user")
    base_metadata: Dict[str, Any] = {"mode": "chat", "session_id": session_id}
    if metadata:
        base_metadata.update(metadata)
    Laminar.set_trace_metadata(base_metadata)

    server = _build_mcp_server()
    agents = _build_subagents(message=message)
    tool_guard = _build_tool_guard(set(agents.keys()))

    options = ClaudeAgentOptions(
        model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
        system_prompt=CHAT_SYSTEM_PROMPT,
        mcp_servers={"liveable": server},
        allowed_tools=[
            "Task",
            "mcp__liveable__geocode_zip",
            "mcp__liveable__get_geo_profile",
            "mcp__liveable__search_nearby_amenities",
            "mcp__liveable__get_census_demographics",
            "mcp__liveable__get_walkscore",
            "mcp__liveable__get_hud_fmr",
            "mcp__liveable__get_rentcast_market",
            "mcp__liveable__get_rentcast_sale_listings",
            "mcp__liveable__search_noise_proxies",
            "mcp__liveable__search_overpass_amenities",
            "mcp__liveable__search_osm_amenities",
            "mcp__liveable__search_housing_prices",
            "mcp__liveable__search_new_developments",
            "mcp__liveable__search_crime_safety",
        ],
        agents=agents,
        can_use_tool=tool_guard,
        max_turns=14,
    )

    prompt = _build_chat_prompt(message, history=history, context=context)

    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        response = await _collect_report(client, stream_callback=stream_callback)

    trace_complete(mode="chat", status="ok")
    trace_id = Laminar.get_trace_id()
    return LiveableResult(report_markdown=response, trace_id=trace_id, session_id=session_id)
