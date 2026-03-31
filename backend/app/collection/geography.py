"""Small, evidence-backed helpers for worldwide trip context."""

from __future__ import annotations

import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models.enums import DestType, TripScope
from app.models.memory import TripMemory

_CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")


def is_local_city_trip(memory: TripMemory) -> bool:
    """Recognize sightseeing within the same explicitly named city."""

    destination = memory.needed.destination
    origin = memory.needed.origin
    return bool(
        origin and destination and destination.type in (DestType.CITY, DestType.CITIES)
        and len(destination.items) == 1
        and origin.strip().casefold() == destination.items[0].strip().casefold()
    )


def normalize_currency_code(value: object) -> str | None:
    """Accept only a three-letter ISO-style currency code, never a guess."""

    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    return normalized if _CURRENCY_CODE.fullmatch(normalized) else None


def apply_researched_geography(memory: TripMemory, research: Any) -> None:
    """Store global context produced by the existing internet-aware research call.

    User-stated budget currency always wins. Other values are filled only when
    absent, so uncertain research cannot replace already-confirmed context.
    """

    if memory.metadata.currency_code is None:
        currency_code = normalize_currency_code(
            getattr(research, "suggested_budget_currency", None)
        )
        if currency_code is not None:
            memory.metadata.currency_code = currency_code

    if memory.metadata.timezone is None:
        timezone = getattr(research, "primary_destination_timezone", None)
        if _is_iana_timezone(timezone):
            memory.metadata.timezone = timezone

    if memory.metadata.trip_scope is None:
        scope = getattr(research, "trip_scope", None)
        try:
            memory.metadata.trip_scope = TripScope(scope)
        except (TypeError, ValueError):
            pass


def clear_location_derived_context(memory: TripMemory) -> None:
    """Remove location-derived values when origin or destination changes."""

    memory.metadata.timezone = None
    memory.metadata.trip_scope = None


def _is_iana_timezone(value: object) -> bool:
    """Accept installed IANA identifiers, including daylight-saving rules."""

    if not isinstance(value, str) or not value.strip():
        return False
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return False
    return True
