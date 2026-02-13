"""Build deterministic constraints from Riya's trusted planning context."""

from __future__ import annotations

from collections.abc import Iterable

from app.models.constraints import (
    ConstraintBundle,
    ImmutableTripConstraints,
    PlanningLimits,
    RelaxationPolicy,
)
from app.models.context import PlanningContext, ResolvedDestination


def build_constraint_bundle(
    context: PlanningContext,
    destination: ResolvedDestination,
    policy: RelaxationPolicy | None = None,
) -> ConstraintBundle:
    """Freeze values the solver must preserve and expose allowed relaxations."""

    required_stops = _unique(stop.name for stop in destination.stops)
    destination_request = _unique(context.destination.items)
    return ConstraintBundle(
        immutable=ImmutableTripConstraints(
            origin=context.origin,
            destination_request=destination_request,
            required_stops=required_stops,
            start_date=context.start_date,
            end_date=context.end_date,
            traveller_count=context.traveller_count,
            trip_budget=context.trip_budget,
            day_start_time=context.day_start_time,
            day_end_time=context.day_end_time,
            end_location=context.end_location,
            allowed_long_distance_modes=_allowed_modes(
                context.long_distance_modes,
                fallback=["train", "car", "flight"],
            ),
            allowed_local_modes=_allowed_modes(
                context.local_transport_modes,
                fallback=["taxi", "car", "metro", "walking", "public_transport"],
            ),
            must_see=context.must_see,
            dietary_restrictions=context.dietary_restrictions,
            accessibility_needs=context.accessibility_needs,
            safety_avoidances=context.avoid,
        ),
        limits=PlanningLimits(
            day_start_time=context.day_start_time,
            day_end_time=context.day_end_time,
            budget=context.trip_budget,
            currency_code=context.currency_code,
            allowed_long_distance_modes=_allowed_modes(
                context.long_distance_modes,
                fallback=["train", "car", "flight"],
            ),
            allowed_local_modes=_allowed_modes(
                context.local_transport_modes,
                fallback=["taxi", "car", "metro", "walking", "public_transport"],
            ),
            must_see=context.must_see,
        ),
        policy=policy or RelaxationPolicy(),
        trip_context_notes=list(context.trip_context_notes),
        field_sources=dict(context.field_sources),
    )


def _allowed_modes(values: list[str], *, fallback: list[str] | str) -> list[str]:
    fallback_list = [fallback] if isinstance(fallback, str) else list(fallback)
    return _unique(values) or fallback_list


def _unique(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result
