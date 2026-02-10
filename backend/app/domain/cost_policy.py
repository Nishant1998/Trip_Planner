"""Conservative MVP cost ranges; LLM-provided numbers are never trusted."""

from __future__ import annotations

from collections import Counter
from math import ceil

from app.models.draft import DraftCost, DraftStay, ItineraryDraft
from app.models.evidence import (
    CostBaselineEvidence,
    EvidenceBundle,
    PlaceEvidence,
)
from app.models.context import (
    PlanningContext,
    ResolvedDestination,
    TripSkeleton,
)

_RANGES = {
    "INR": {
        "meal": (200, 500),
        "activity": (0, 800),
        "low_cost_activity": (0, 200),
        "room": (1200, 2500),
    },
    "JPY": {
        "meal": (900, 2500),
        "activity": (0, 3000),
        "low_cost_activity": (0, 800),
        "room": (8000, 18000),
    },
    "USD": {
        "meal": (12, 35),
        "activity": (0, 40),
        "low_cost_activity": (0, 10),
        "room": (70, 180),
    },
}


def apply_cost_policy(
    draft: ItineraryDraft,
    context: PlanningContext,
    destination: ResolvedDestination,
    evidence: EvidenceBundle,
    skeleton: TripSkeleton | None = None,
) -> ItineraryDraft:
    """Return a copy whose cost numbers come only from declared policy."""
    updated = draft.model_copy(deep=True)
    if skeleton is not None:
        _ensure_required_stays(updated, skeleton, context.currency_code)
    places = evidence.place_map()
    for day in updated.days:
        for activity in day.activities:
            stop_name = _day_stop_name(destination, day.day_number, skeleton)
            currency = _stop_currency(destination, stop_name, context.currency_code)
            policy = _policy(currency)
            source_ids: list[str] = []
            basis_detail = "declared conservative fallback policy"
            if activity.kind == "meal":
                baseline = _baseline(evidence, stop_name, "meal")
                if baseline is not None:
                    minimum, maximum = float(baseline.minimum), float(baseline.maximum)
                    currency = baseline.currency_code
                    source_ids = baseline.source_ids
                    basis_detail = f"current web baseline ({baseline.unit})"
                else:
                    minimum, maximum = policy["meal"]
                units = context.traveller_count
                category = "meal"
            elif activity.kind == "activity" and activity.evidence_place_id:
                place = places.get(activity.evidence_place_id)
                category = "low_cost_activity" if _is_low_cost(place) else "activity"
                minimum, maximum = policy[category]
                if _is_budget_trip(context) and currency == "INR":
                    maximum = min(maximum, 500)
                units = context.traveller_count
            else:
                activity.cost = None
                continue
            activity.cost = DraftCost(
                minimum=minimum * units,
                maximum=maximum * units,
                currency_code=currency,
                basis=(
                    f"Conservative MVP {category.replace('_', ' ')} {basis_detail} for "
                    f"{units} traveller(s); confirm current prices before booking."
                ),
                source_ids=source_ids,
            )

    rooms = max(1, ceil(context.traveller_count / 2))
    for stay in updated.stays:
        currency = _stop_currency(destination, stay.stop_name, context.currency_code)
        baseline = _baseline(evidence, stay.stop_name, "lodging")
        stay_source_ids: list[str] = []
        basis_detail = "declared conservative fallback policy"
        if baseline is not None:
            minimum, maximum = float(baseline.minimum), float(baseline.maximum)
            currency = baseline.currency_code
            stay_source_ids = baseline.source_ids
            basis_detail = f"current web baseline ({baseline.unit})"
        else:
            minimum, maximum = _policy(currency)["room"]
        units = rooms * stay.nights
        stay.cost = DraftCost(
            minimum=minimum * units,
            maximum=maximum * units,
            currency_code=currency,
            basis=(
                f"Conservative MVP lodging {basis_detail} for {rooms} room(s) over "
                f"{stay.nights} night(s); no booking or availability is implied."
            ),
            source_ids=stay_source_ids,
        )
    return updated


def _ensure_required_stays(
    draft: ItineraryDraft,
    skeleton: TripSkeleton,
    currency_code: str,
) -> None:
    nights = Counter(day.base_stop for day in skeleton.days[:-1])
    existing = {stay.stop_name.casefold(): stay for stay in draft.stays}
    required: list[DraftStay] = []
    for stop_name, night_count in nights.items():
        stay = existing.get(stop_name.casefold())
        if stay is None:
            stay = DraftStay(
                stop_name=stop_name,
                area=f"Central {stop_name}",
                nights=night_count,
                description="Approximate routing base; choose a suitable property.",
                cost=DraftCost(
                    currency_code=currency_code,
                    basis="Placeholder replaced by deterministic lodging policy.",
                ),
            )
        else:
            stay = stay.model_copy(
                update={"stop_name": stop_name, "nights": night_count}
            )
        required.append(stay)
    draft.stays = required


def _day_stop_name(
    destination: ResolvedDestination,
    day_number: int,
    skeleton: TripSkeleton | None,
) -> str:
    if skeleton is not None:
        day = next(
            (item for item in skeleton.days if item.day_number == day_number),
            None,
        )
        if day is not None:
            return day.base_stop
    if not destination.stops:
        return ""
    index = min(len(destination.stops) - 1, max(0, day_number - 1))
    return destination.stops[index].name


def _stop_currency(
    destination: ResolvedDestination,
    stop_name: str,
    fallback: str,
) -> str:
    for stop in destination.stops:
        if stop.name.casefold() == stop_name.casefold():
            return stop.currency_code or fallback
    return fallback


def _policy(currency: str) -> dict[str, tuple[int, int]]:
    return _RANGES.get(currency.upper(), _RANGES["USD"])


def _baseline(
    evidence: EvidenceBundle,
    stop_name: str,
    category: str,
) -> CostBaselineEvidence | None:
    return next(
        (
            item
            for item in evidence.cost_baselines
            if item.stop_name.casefold() == stop_name.casefold()
            and item.category.casefold() == category.casefold()
        ),
        None,
    )


def _is_low_cost(place: PlaceEvidence | None) -> bool:
    if place is None:
        return False
    text = " ".join(place.categories).casefold()
    return any(
        value in text
        for value in ("park", "garden", "beach", "temple", "church", "market")
    )


def _is_budget_trip(context: PlanningContext) -> bool:
    tier = " ".join(_requirement_values(context, "spending_tier")).casefold()
    lodging = " ".join(_requirement_values(context, "lodging_type")).casefold()
    return "budget" in tier or "hostel" in lodging


def _requirement_values(context: PlanningContext, name: str) -> list[str]:
    value = context.requirements.get(name)
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]
