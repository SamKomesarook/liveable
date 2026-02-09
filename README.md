# Liveable — Neighborhood Analysis Agent

Liveable is a Python CLI assistant that takes a US ZIP code and answers neighborhood questions conversationally. It uses the Claude Agent SDK with Laminar observability instrumentation and custom MCP tools for each data source.

## Features
- Claude Agent SDK orchestration + MCP tools
- Laminar tracing for every tool call and synthesis step
- Google Places, Census ACS, Walk Score, RentCast, HUD FMR, Overpass, and web crime search integrations
- Conversational REPL with tool-backed answers

## Requirements
- Python 3.10+
- Anthropic API key (`ANTHROPIC_API_KEY`)
- API keys for Google Places, Census, and Walk Score
- Laminar project API key
- Optional: RentCast, HUD FMR, and Serper API keys for housing/crime sections

## Setup
```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Create a `.env` file from `.env.example` and fill in the values.

## Running
```bash
python -m liveable
```

Ask questions interactively. Use `/set 20001` to load a location or include a ZIP in your question. Type `exit` to quit.

Compare two locations:
```
/compare 20001 20878
```

## API Keys (Where to get them)
Required:
- Anthropic: https://console.anthropic.com/
- Laminar: https://www.lmnr.ai/
- Google Places (Maps Platform): https://console.cloud.google.com/apis/credentials
- Census API: https://api.census.gov/data/key_signup.html
- Walk Score: https://www.walkscore.com/professional/api-sign-up.php

Optional (recommended for richer answers):
- RentCast: https://www.rentcast.io/api
- HUD User API (FMR): https://www.huduser.gov/portal/datasets/hudapi.html
- Browser Use Cloud: https://cloud.browser-use.com

No key required:
- Overpass API (OSM) — used for noise proxies and amenity counts (https://overpass-api.de/api/interpreter)

## Environment Variables
See `.env.example` for the full list. Optional variables:
- `CLAUDE_MODEL` — override the Claude model (defaults to `claude-sonnet-4-20250514`)
- `LIVEABLE_USER_ID` — sets Laminar trace user id (defaults to your OS user)
- `LMNR_TRACE_BASE_URL` — override the trace base URL for printing
- `HUD_API_KEY` — HUD User API key for Fair Market Rent data
- `RENTCAST_API_KEY` — RentCast API key for sales/listings and market stats
- `BROWSER_USE_API_KEY` — Browser Use Cloud API key for crime data web search
- `OVERPASS_URL` — optional override for Overpass endpoint (defaults to https://overpass.private.coffee/api/interpreter)
- `DEV_PERMITS_BASE_URL` — optional open-data endpoint for permits/developments
- `DEV_PERMITS_QUERY` — optional query template for filtering permits by zip/city

## Notes
- If any data source fails, the agent will note reduced confidence in its answers.
- Development pipeline data requires an open-data endpoint; set `DEV_PERMITS_BASE_URL` to enable it.

## License
MIT. See `LICENSE`.
