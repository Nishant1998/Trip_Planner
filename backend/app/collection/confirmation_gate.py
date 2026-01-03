"""Deterministic final-summary and approval-state helpers."""

from __future__ import annotations

from hashlib import sha256
import json

from app.models.memory import ConfirmationState, TripMemory


def build_confirmation_summary(memory: TripMemory) -> str:
    """Build the complete, readable trip record the user is asked to approve."""

    destination = memory.needed.destination
    destinations = ", ".join(destination.items) if destination else "Not selected"
    end_location = memory.optional.end_location
    if isinstance(end_location, str) and end_location.casefold() == "origin":
        end_text = f"Return to {memory.needed.origin}"
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
        f"Route: {memory.needed.origin or 'Not set'} → {destinations}",
        f"Dates: {memory.needed.start_date or 'Not set'} to {memory.needed.end_date or 'Not set'} ({duration})",
        f"Travellers: {', '.join(traveller_parts)}",
        f"Total budget: {budget if budget is not None else 'Not set'} {currency}",
        end_text,
    ]
    if memory.optional.travel_style is not None:
        lines.append(f"Pace: {memory.optional.travel_style.value}")
    travel_modes = _travel_modes(memory)
    if travel_modes:
        lines.append(f"Travel: {travel_modes}")
    if memory.optional.activity_interests:
        lines.append("Interests: " + ", ".join(memory.optional.activity_interests))
    if memory.optional.must_see:
        lines.append("Must-see: " + ", ".join(memory.optional.must_see))
    if memory.optional.dietary_restrictions:
        lines.append("Food needs: " + ", ".join(memory.optional.dietary_restrictions))
    if memory.optional.accessibility_needs:
        lines.append(
            "Mobility needs: " + "; ".join(memory.optional.accessibility_needs)
        )
    if memory.optional.avoid:
        lines.append("Avoid: " + "; ".join(memory.optional.avoid))
    notes = _summary_notes(memory)
    if notes:
        lines.append("Important notes: " + "; ".join(notes))
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
    """Show only distinct notes that add something beyond visible fields."""

    visible = " ".join(
        str(value).casefold()
        for value in (
            memory.needed.origin,
            memory.needed.destination.items if memory.needed.destination else None,
            memory.needed.start_date,
            memory.needed.end_date,
            memory.needed.trip_duration_days,
            memory.needed.num_adults,
            memory.needed.trip_budget,
            memory.optional.travel_style,
            memory.optional.activity_interests,
            memory.optional.dietary_restrictions,
            memory.optional.accessibility_needs,
            memory.optional.avoid,
        )
        if value is not None
    )
    unique: list[str] = []
    seen: set[str] = set()
    for note in _friendly_notes(memory.internal.trip_context_notes):
        normalized = " ".join(note.casefold().split())
        if not normalized or normalized in seen:
            continue
        keywords = [word for word in normalized.split() if len(word) > 4]
        if keywords and all(word in visible for word in keywords):
            continue
        seen.add(normalized)
        unique.append(note)
    return unique


def _travel_modes(memory: TripMemory) -> str | None:
    """Format only the user's meaningful broad transport choices."""

    modes = memory.optional.long_distance_modes or []
    labels = [getattr(mode, "value", str(mode)) for mode in modes]
    labels = [label for label in labels if label != "any"]
    return ", ".join(labels) if labels else None
