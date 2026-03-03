"""Run one planning tool for deliberate, user-controlled end-to-end checks."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from dotenv import dotenv_values

from .bright_data import parse_hotel_output_fields
from .catalog import PlanningToolCatalog


def _configuration() -> dict[str, bool]:
    from app.config import ENV_FILE
    values = dotenv_values(ENV_FILE)

    def set_(name: str) -> bool:
        return bool(os.getenv(name) or values.get(name))

    try:
        hotel_fields_valid = parse_hotel_output_fields(
            os.getenv("BRIGHT_DATA_HOTELS_OUTPUT_FIELDS") or values.get("BRIGHT_DATA_HOTELS_OUTPUT_FIELDS")) is not None
    except ValueError:
        hotel_fields_valid = False

    return {
        "google_key_present": set_("GOOGLE_MAPS_API_KEY"),
        "open_meteo_key_required": False,
        "bright_data_token_present": set_("BRIGHT_DATA_API_TOKEN"),
        "flight_scraper_configured": set_("BRIGHT_DATA_API_TOKEN"),
        "hotel_output_mapping_valid": hotel_fields_valid,
        "hotel_scraper_configured": all(set_(name) for name in (
            "BRIGHT_DATA_API_TOKEN", "BRIGHT_DATA_HOTELS_DATASET_ID",
            "BRIGHT_DATA_HOTELS_INPUT_TEMPLATE")) and hotel_fields_valid,
        "train_bus_provider_configured": set_("GOOGLE_MAPS_API_KEY"),
    }


async def _invoke(tool: str, payload: dict) -> str:
    catalog = PlanningToolCatalog.from_local_env()
    try:
        result = await catalog.agent_tools()[tool](payload)
        # ASCII escapes keep CLI output writable on Windows consoles whose
        # code page cannot represent hotel names from every destination.
        return json.dumps(result.model_dump(mode="json"), ensure_ascii=True, indent=2)
    finally:
        await catalog.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run exactly one Trip Planner tool")
    parser.add_argument("--config", action="store_true", help="show only configuration presence flags")
    parser.add_argument("--tool", choices=("places", "maps", "weather", "transport", "accommodation"))
    parser.add_argument("--input", help="one JSON object matching the selected tool input")
    parser.add_argument("--live", action="store_true", help="allow provider requests for one tool call (may cost money)")
    args = parser.parse_args(argv)
    if args.config:
        print(json.dumps(_configuration(), indent=2))
        return 0
    if not args.tool or not args.input:
        print("Specify --tool and --input, or use --config.", file=sys.stderr)
        return 2
    try:
        payload = json.loads(args.input)
    except json.JSONDecodeError:
        print("Invalid JSON input.", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("Input must be one JSON object.", file=sys.stderr)
        return 2
    if not args.live:
        print("Add --live to authorize a provider call for this one tool.", file=sys.stderr)
        return 2
    try:
        print(asyncio.run(_invoke(args.tool, payload)))
        return 0
    except Exception as exc:
        # Avoid echoing provider URLs, request headers, tokens, or untrusted input.
        print(f"Tool input/configuration error ({type(exc).__name__}).", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
