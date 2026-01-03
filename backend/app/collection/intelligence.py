# Fetches and caches combined destination intelligence.
"""Build and retain combined destination intelligence for a trip."""

from __future__ import annotations

import hashlib
import json
import logging

from app.collection.geography import apply_researched_geography
from app.models.memory import TripMemory

logger = logging.getLogger(__name__)


def destination_research_snapshot(memory: TripMemory) -> str:
    """Fingerprint every field that can change destination research."""

    destination = memory.needed.destination
    modes = memory.optional.long_distance_modes or []
    fields = {
        "origin": memory.needed.origin,
        "destinations": destination.items if destination else [],
        "start_date": str(memory.needed.start_date or ""),
        "end_date": str(memory.needed.end_date or ""),
        "long_distance_modes": [mode.value for mode in modes],
    }
    payload = json.dumps(
        fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def maybe_build_destination_intelligence(memory: TripMemory, agents) -> None:
    """Populate combined destination context once, without blocking collection.

    Fires as soon as origin and destination are both known.  The result covers:
      - Soft planning context (duration, best months, suggested transport, budget signal)
      - Route feasibility (is self-driving practical for this route?)
      - Seasonal timing (any meaningful concerns for the travel date?)

    The cache is cleared in the orchestrator whenever origin, destination, or
    start_date changes so the agent re-runs with fresh inputs.
    """

    origin = memory.needed.origin
    destination = memory.needed.destination
    agent = getattr(agents, "destination_intelligence", None)
    if not origin or not destination or not destination.items or agent is None:
        return

    state = memory.internal.sanity_state
    snapshot = destination_research_snapshot(memory)
    if (
        state.destination_intelligence is not None
        and state.destination_intelligence_snapshot == snapshot
    ):
        return

    # Never reuse research produced for different endpoints, dates, or modes.
    state.destination_intelligence = None
    state.destination_intelligence_snapshot = None

    # Optional context — included when available to enable route and timing checks.
    start_date = memory.needed.start_date
    start_date_str = start_date.isoformat() if start_date else "unknown"
    end_date = memory.needed.end_date
    end_date_str = end_date.isoformat() if end_date else start_date_str

    transport = memory.optional.long_distance_modes  # list[TransportLong] | None
    modes_str = ", ".join(m.value for m in transport) if transport else "not specified"

    known_currency = memory.metadata.currency_code or "not stated"
    task = (
        f"Origin: {origin}\n"
        f"Destination(s): {', '.join(destination.items)}\n"
        f"Travel dates: {start_date_str} to {end_date_str}\n"
        f"Selected long-distance modes: {modes_str}\n"
        f"Budget currency stated by traveller: {known_currency}"
    )

    try:
        result = await agent.run(task)
        apply_researched_geography(memory, result)
        state.destination_intelligence = result.model_dump()
        state.destination_intelligence_snapshot = snapshot
        logger.info(
            "Destination intelligence cached for %s to %s (start=%s, modes=%s)",
            origin,
            destination.items,
            start_date_str,
            modes_str,
        )
    except Exception as exc:  # Non-fatal by design: collection still works.
        logger.warning("Destination intelligence unavailable: %s", exc)
