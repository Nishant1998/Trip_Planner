"""Deterministic final-summary and approval-state helpers."""

from __future__ import annotations

from hashlib import sha256
import json
import re

from app.models.memory import ConfirmationState, TripMemory
from app.models.enums import FieldSource
from app.collection.geography import is_local_city_trip

_MONEY = r"(?:(?:[₹$€£]|INR|USD|EUR|GBP|Rs\.?)\s*)?\d[\d,.]*(?:\s*(?:INR|USD|EUR|GBP|rupees))?"
_BUDGET_SCOPE_PREFIX = re.compile(
    rf"(\btotal(?:\s+trip)?\s+budget)(?:\s+of|\s+is|\s*:)?\s*{_MONEY}(?=\s+(?:includes|covers|excludes)\b)",
    re.IGNORECASE,
)
_BUDGET_SCOPE_SUFFIX = re.compile(
    rf"{_MONEY}\s+(?=total(?:\s+trip)?\s+budget\b)",
    re.IGNORECASE,
)


def normalize_budget_scope_notes(memory: TripMemory) -> None:
    """Keep the budget amount in its field and the inclusions in the notes.

    A copied total becomes stale after an edit. Independent hotel quotes,
    per-person limits and other prices are left untouched.
    """

    def scope_only(note: str) -> str:
        note = _BUDGET_SCOPE_PREFIX.sub(r"\1", note)
        return _BUDGET_SCOPE_SUFFIX.sub("", note)

    memory.internal.trip_context_notes = list(
        dict.fromkeys(scope_only(note) for note in memory.internal.trip_context_notes)
    )
    if memory.optional.trip_notes is not None:
        memory.optional.trip_notes = list(
            dict.fromkeys(scope_only(note) for note in memory.optional.trip_notes)
        )


def build_confirmation_summary(memory: TripMemory) -> str:
    """Build the complete, readable trip record the user is asked to approve."""

    destination = memory.needed.destination
    destinations = ", ".join(destination.items) if destination else "Not selected"
    local_trip = is_local_city_trip(memory)
    end_location = memory.optional.end_location
    if isinstance(end_location, str) and end_location.casefold() == "origin":
        end_text = (
            f"Start and finish in {memory.needed.origin}"
            if local_trip
            else f"Return to {memory.needed.origin}"
        )
    elif end_location:
        end_text = f"Finish in {end_location}"
    else:
        end_text = "Trip ending point not set"

    traveller_parts = [f"{memory.needed.num_adults or 0} adult(s)"]
    if memory.optional.num_children:
        traveller_parts.append(f"{memory.optional.num_children} child(ren)")
    if memory.optional.num_seniors:
        traveller_parts.append(f"{memory.optional.num_seniors} senior(s)")

    budget = memory.needed.trip_budget
    currency = memory.metadata.currency_code or "currency not yet confirmed"
    duration = f"{memory.needed.trip_duration_days or 'unknown'} days"
    if memory.internal.duration_display_label:
        duration = f"{memory.internal.duration_display_label} · {duration}"
    lines = [
        "Here’s the trip I’ll plan:",
        f"Sightseeing in {destinations}"
        if local_trip
        else f"Route: {memory.needed.origin or 'Not set'} → {destinations}",
        f"Dates: {memory.needed.start_date or 'Not set'} to {memory.needed.end_date or 'Not set'} ({duration})",
        f"Travellers: {', '.join(traveller_parts)}",
        f"Total budget: {format(budget, ',') if budget is not None else 'Not set'} {currency}",
        end_text,
    ]
    if memory.optional.travel_style is not None:
        lines.append(f"Pace: {memory.optional.travel_style.value}")
    travel_modes = _travel_modes(memory.optional.long_distance_modes)
    if travel_modes:
        lines.append(f"Travel: {travel_modes}")
    local_modes = _travel_modes(memory.optional.local_transport_modes)
    if local_modes and local_modes != travel_modes:
        lines.append(f"Getting around: {local_modes}")
    if memory.optional.activity_interests:
        lines.append("Interests: " + ", ".join(memory.optional.activity_interests))
    if memory.optional.must_see:
        lines.append("Must-see: " + ", ".join(memory.optional.must_see))
    if memory.optional.cuisine_prefs:
        lines.append("Food preferences: " + ", ".join(memory.optional.cuisine_prefs))
    if memory.optional.dietary_restrictions:
        lines.append(
            "Dietary restrictions: " + ", ".join(memory.optional.dietary_restrictions)
        )
    elif memory.optional.get_source("dietary_restrictions") == FieldSource.USER:
        lines.append("Other dietary restrictions: none stated")
    for field, label in (
        ("day_start_time", "Daily sightseeing starts"),
        ("day_end_time", "Daily sightseeing ends"),
        ("depart_time_window", "Outbound departure preference"),
        ("arrival_time_window", "Destination arrival preference"),
        ("return_time_window", "Return departure preference"),
        ("hotel_tier", "Hotel comfort"),
        ("lodging_type", "Accommodation"),
        ("room_type_pref", "Room preference"),
    ):
        value = memory.get_field_value(field)
        if (
            value is not None
            and memory.optional.get_source(field) != FieldSource.DEFAULT
        ):
            label_value = _friendly_label(value)
            if label_value != "any":
                lines.append(f"{label}: {label_value}")
    if memory.optional.accessibility_needs:
        lines.append(
            "Mobility needs: " + "; ".join(memory.optional.accessibility_needs)
        )
    if memory.optional.avoid:
        lines.append("Avoid: " + "; ".join(memory.optional.avoid))
    notes = _summary_notes(memory)
    if notes:
        lines.append("Important notes:\n" + "\n".join(f"• {note}" for note in notes))
    lines.append(
        "Does this look right? Reply yes to start planning, or tell me what you’d like to change."
    )
    return "\n".join(lines)


def mark_confirmation_summary_shown(memory: TripMemory) -> None:
    """Record that the current user-visible trip record awaits approval."""

    memory.internal.confirmation_state = ConfirmationState(
        active=True,
        summary_fingerprint=confirmation_fingerprint(memory),
    )


def invalidate_confirmation_summary(memory: TripMemory) -> None:
    """Ensure an edited trip must be shown again before planning can begin."""

    memory.internal.confirmation_state = ConfirmationState()


def has_current_confirmation_summary(memory: TripMemory) -> bool:
    """Return whether the exact current trip details were shown for approval."""

    state = memory.internal.confirmation_state
    return state.active and state.summary_fingerprint == confirmation_fingerprint(
        memory
    )


def confirmation_fingerprint(memory: TripMemory) -> str:
    """Fingerprint the user-visible trip details, excluding internal runtime state."""

    payload = memory.to_canonical_dict()
    visible = {
        "metadata": payload["metadata"],
        "needed": payload["needed"],
        "optional": payload["optional"],
        "trip_context_notes": memory.internal.trip_context_notes,
        "booking_note": memory.internal.sanity_state.pre_summary_gate.booking_window_note,
    }
    encoded = json.dumps(
        visible, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _friendly_notes(notes: list[str]) -> list[str]:
    """Keep user constraints while hiding internal hard/soft classification."""

    return [
        note.removeprefix("[HARD] ").removeprefix("[SOFT] ")
        for note in notes
        if note.strip()
    ]


def _summary_notes(memory: TripMemory) -> list[str]:
    """Deduplicate notes without discarding constraints based on word overlap."""

    notes = [*memory.internal.trip_context_notes, *(memory.optional.trip_notes or [])]
    booking_note = memory.internal.sanity_state.pre_summary_gate.booking_window_note
    if booking_note:
        notes.append(booking_note)
    unique: list[str] = []
    seen: set[str] = set()
    for note in _friendly_notes(notes):
        normalized = " ".join(note.casefold().split())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(note)
    return unique


def _friendly_label(value) -> str:
    text = getattr(value, "value", str(value))
    return "own car (self-drive)" if text == "self_driving" else text.replace("_", " ")


def _travel_modes(modes) -> str | None:
    labels = [_friendly_label(mode) for mode in modes or []]
    labels = [label for label in labels if label != "any"]
    return ", ".join(labels) if labels else None
