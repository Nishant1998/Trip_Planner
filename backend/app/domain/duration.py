"""Generic, evidence-first visit and operational-time policy."""

from __future__ import annotations

from app.models.duration import VisitDuration
from app.models.evidence import FactStatus, PlaceEvidence, VisitDurationEvidence
from app.models.context import PlanningContext


def estimate_visit_duration(
    place: PlaceEvidence,
    context: PlanningContext,
    requested_minutes: int,
    evidence: VisitDurationEvidence | None = None,
) -> VisitDuration:
    """Estimate one visit without rules tied to any particular venue category."""

    if evidence is not None:
        minimum = evidence.minimum_minutes
        recommended = evidence.recommended_minutes
        maximum = evidence.maximum_minutes
    else:
        # Provider defaults occasionally contain outliers. Keep the generic fallback
        # bounded without inferring anything from the venue category.
        baseline = min(180, max(30, requested_minutes))
        minimum = max(15, round(baseline * 0.70))
        recommended = baseline
        maximum = max(recommended, round(baseline * 1.50))

    slower_pace = bool(
        context.num_seniors
        or context.accessibility_needs
        or context.travel_style.casefold() in {"relaxed", "slow", "leisurely"}
    )
    if slower_pace:
        recommended = round(recommended * 1.15)
        maximum = round(maximum * 1.15)

    # Missing operational components use a small, venue-neutral policy allowance.
    # A cited component replaces only that component, so one known access time does
    # not accidentally erase the waiting and exit allowances.
    default_access, default_waiting, default_exit = (
        (7, 8, 5) if slower_pace else (5, 5, 5)
    )
    access = (
        evidence.access_minutes
        if evidence is not None and evidence.access_minutes is not None
        else default_access
    )
    waiting = (
        evidence.waiting_minutes
        if evidence is not None and evidence.waiting_minutes is not None
        else default_waiting
    )
    exit_minutes = (
        evidence.exit_minutes
        if evidence is not None and evidence.exit_minutes is not None
        else default_exit
    )
    if evidence is not None and evidence.rest_minutes is not None:
        rest = evidence.rest_minutes
    elif evidence is not None and evidence.physical_effort == "high":
        rest = 25 if slower_pace else 15
    elif (
        evidence is not None and evidence.physical_effort == "moderate" and slower_pace
    ):
        rest = 10
    else:
        rest = 0

    scheduled = min(max(requested_minutes, recommended), maximum)
    operational_total = access + waiting + rest + exit_minutes
    return VisitDuration(
        minimum_minutes=minimum,
        recommended_minutes=recommended,
        maximum_minutes=maximum,
        scheduled_minutes=scheduled,
        activity_buffer_minutes=operational_total,
        access_minutes=access,
        waiting_minutes=waiting,
        rest_minutes=rest,
        exit_minutes=exit_minutes,
        physical_effort=evidence.physical_effort if evidence is not None else None,
        last_entry_minutes_before_close=(
            evidence.last_entry_minutes_before_close if evidence is not None else None
        ),
        confidence=evidence.confidence if evidence is not None else "low",
        status=evidence.status if evidence is not None else FactStatus.ESTIMATED,
        basis=(
            f"{evidence.basis} Operational components use cited values where available "
            "and generic uncertainty allowances for missing components."
            if evidence is not None
            else "Provider visit estimate plus generic arrival, waiting, and exit uncertainty."
        ),
        source_ids=evidence.source_ids if evidence is not None else [],
    )
