"""Conservative cost estimates for deterministic V3 candidates."""

from __future__ import annotations

from decimal import Decimal
from math import ceil

from app.models.draft import DraftCost
from app.models.evidence import (
    CostBaselineEvidence,
    EvidenceBundle,
    FactStatus,
    PlaceEvidence,
    RouteEvidence,
)
from app.models.context import PlanningContext
from app.models.plan import MoneyRange
from app.domain.expenses import convert_cost


def route_cost(
    route: RouteEvidence,
    context: PlanningContext,
    evidence: EvidenceBundle,
    stop_name: str,
) -> MoneyRange:
    """Prefer provider/current-fact ranges; otherwise use a disclosed fallback."""

    if (
        route.minimum_cost is not None
        and route.maximum_cost is not None
        and route.cost_currency_code
    ):
        return convert_cost(
            DraftCost(
                minimum=float(route.minimum_cost),
                maximum=float(route.maximum_cost),
                currency_code=route.cost_currency_code,
                basis=route.cost_basis
                or "Provider mode-level planning range; not a quote.",
                source_ids=route.source_ids,
            ),
            context.currency_code,
            evidence.currency_rates,
        )

    baseline = _baseline(evidence, stop_name, "local_transport")
    # The baseline unit is explicitly local travel. Applying it to a 100 km
    # intercity leg greatly overstates cost, so it is bounded to local routes.
    if (
        baseline is not None
        and route.distance_km is not None
        and route.distance_km <= 25
    ):
        units = max(Decimal("1"), Decimal(str(route.distance_km)) / Decimal("5"))
        return convert_cost(
            DraftCost(
                minimum=float(baseline.minimum * units),
                maximum=float(baseline.maximum * units),
                currency_code=baseline.currency_code,
                basis=(
                    f"Current transport baseline ({baseline.unit}) scaled by route distance; "
                    "not a live fare."
                ),
                source_ids=list(
                    dict.fromkeys([*baseline.source_ids, *route.source_ids])
                ),
            ),
            context.currency_code,
            evidence.currency_rates,
        )

    distance = Decimal(str(route.distance_km or 5))
    per_km = _fallback_transport_rate(context.currency_code, route.mode)
    multiplier = (
        Decimal(context.traveller_count)
        if _is_per_person_mode(route.mode)
        else Decimal(1)
    )
    return MoneyRange(
        minimum=(distance * per_km[0] * multiplier).quantize(Decimal("0.01")),
        maximum=(distance * per_km[1] * multiplier).quantize(Decimal("0.01")),
        currency_code=context.currency_code,
        status=FactStatus.ESTIMATED,
        basis="Declared conservative fallback based on route distance; confirm before travel.",
        source_ids=route.source_ids,
    )


def activity_cost(
    place: PlaceEvidence,
    context: PlanningContext,
    evidence: EvidenceBundle,
) -> MoneyRange:
    baseline = _baseline(evidence, place.stop_name, "activity")
    if baseline is not None:
        return _per_traveller_baseline(baseline, context, evidence)
    low_cost = any(
        label in " ".join(place.categories).casefold()
        for label in ("park", "garden", "beach", "temple", "church", "market")
    )
    fallback = _currency_range(
        context.currency_code,
        (Decimal("0"), Decimal("200")) if low_cost else (Decimal("0"), Decimal("800")),
    )
    return MoneyRange(
        minimum=fallback[0] * context.traveller_count,
        maximum=fallback[1] * context.traveller_count,
        currency_code=context.currency_code,
        status=FactStatus.ESTIMATED,
        basis="Conservative category fallback; verify admission price before visiting.",
        source_ids=place.source_ids,
    )


def meal_cost(
    stop_name: str,
    context: PlanningContext,
    evidence: EvidenceBundle,
) -> MoneyRange:
    baseline = _baseline(evidence, stop_name, "meal")
    if baseline is not None:
        return _per_traveller_baseline(baseline, context, evidence)
    minimum, maximum = _currency_range(
        context.currency_code,
        (Decimal("250"), Decimal("600")),
    )
    return MoneyRange(
        minimum=minimum * context.traveller_count,
        maximum=maximum * context.traveller_count,
        currency_code=context.currency_code,
        status=FactStatus.ESTIMATED,
        basis="Conservative meal fallback per traveller; no restaurant booking is implied.",
    )


def stay_cost(
    stop_name: str,
    nights: int,
    context: PlanningContext,
    evidence: EvidenceBundle,
) -> MoneyRange:
    rooms = max(1, ceil(context.traveller_count / 2))
    baseline = _baseline(evidence, stop_name, "lodging")
    if baseline is not None:
        units = rooms * nights
        return convert_cost(
            DraftCost(
                minimum=float(baseline.minimum * units),
                maximum=float(baseline.maximum * units),
                currency_code=baseline.currency_code,
                basis=(
                    f"Current lodging baseline ({baseline.unit}) for {rooms} room(s) and "
                    f"{nights} night(s); no availability is implied."
                ),
                source_ids=baseline.source_ids,
            ),
            context.currency_code,
            evidence.currency_rates,
        )
    minimum, maximum = _currency_range(
        context.currency_code,
        (Decimal("1500"), Decimal("3500")),
    )
    return MoneyRange(
        minimum=minimum * rooms * nights,
        maximum=maximum * rooms * nights,
        currency_code=context.currency_code,
        status=FactStatus.ESTIMATED,
        basis=(
            f"Conservative lodging fallback for {rooms} room(s) and {nights} night(s); "
            "no availability is implied."
        ),
    )


def zero_cost(currency_code: str, basis: str) -> MoneyRange:
    return MoneyRange(
        minimum=Decimal("0"),
        maximum=Decimal("0"),
        currency_code=currency_code,
        status=FactStatus.ESTIMATED,
        basis=basis,
    )


def _baseline(
    evidence: EvidenceBundle,
    stop_name: str,
    category: str,
) -> CostBaselineEvidence | None:
    return next(
        (
            value
            for value in evidence.cost_baselines
            if value.stop_name.casefold() == stop_name.casefold()
            and value.category.casefold() == category.casefold()
        ),
        None,
    )


def _per_traveller_baseline(
    baseline: CostBaselineEvidence,
    context: PlanningContext,
    evidence: EvidenceBundle,
) -> MoneyRange:
    return convert_cost(
        DraftCost(
            minimum=float(baseline.minimum * context.traveller_count),
            maximum=float(baseline.maximum * context.traveller_count),
            currency_code=baseline.currency_code,
            basis=f"Current planning baseline ({baseline.unit}); confirm before purchase.",
            source_ids=baseline.source_ids,
        ),
        context.currency_code,
        evidence.currency_rates,
    )


def _currency_range(
    currency_code: str,
    inr_range: tuple[Decimal, Decimal],
) -> tuple[Decimal, Decimal]:
    if currency_code.upper() == "INR":
        return inr_range
    return (inr_range[0] / 80, inr_range[1] / 80)


def _fallback_transport_rate(
    currency_code: str,
    mode: str,
) -> tuple[Decimal, Decimal]:
    normalized = mode.casefold()
    if normalized in {"walk", "walking"}:
        values = (Decimal("0"), Decimal("0"))
    elif normalized in {"train", "rail", "metro", "bus"}:
        values = (Decimal("1"), Decimal("4"))
    elif normalized in {"flight", "plane", "air"}:
        values = (Decimal("4"), Decimal("12"))
    else:
        values = (Decimal("10"), Decimal("24"))
    if currency_code.upper() == "INR":
        return values
    return (values[0] / 80, values[1] / 80)


def _is_per_person_mode(mode: str) -> bool:
    return mode.casefold() in {
        "train",
        "rail",
        "metro",
        "bus",
        "flight",
        "plane",
        "air",
    }
