"""Structural physical-route chains; deliberately independent of timetable logic."""

from __future__ import annotations

from ..models.planning import (
    DayKind,
    DayPlan,
    DayRouteChain,
    PlannerInput,
    RouteLeg,
    TripRoutePlan,
    ValidationIssue,
)
from ..tools.routing import RouteResolver


def _leg_id(origin_id: str, destination_id: str, ordinal: int) -> str:
    return f"leg:{origin_id}:{destination_id}:{ordinal}"


def _physical_node_id(node_id: str) -> str:
    """Return the stable place ID behind an overnight-anchor alias.

    The scheduler labels an overnight stay as ``overnight:<place-id>``.  It is
    still the same physical place as the POI/base with ``<place-id>``; routing
    between the two would manufacture a zero-distance travel leg.
    """
    return node_id.removeprefix("overnight:")


def _same_physical_node(left: str, right: str) -> bool:
    return _physical_node_id(left) == _physical_node_id(right)


def _route_leg(resolver: RouteResolver, fact, ordinal: int) -> RouteLeg:
    decision = fact.mode_resolution
    status = (
        "resolved"
        if fact.duration_minutes is not None
        else ("unsupported" if fact.status == "unsupported_by_provider" else "unknown")
    )
    warnings = (
        []
        if fact.duration_minutes is not None
        else [
            "Travel timing requires confirmation; this physical leg remains explicit."
        ]
    )
    return RouteLeg(
        id=_leg_id(fact.origin_id, fact.destination_id, ordinal),
        origin_node_id=fact.origin_id,
        destination_node_id=fact.destination_id,
        origin_name=resolver.node(fact.origin_id).name,
        destination_name=resolver.node(fact.destination_id).name,
        movement_class=(
            decision.movement_class
            if decision
            else resolver.movement_class(fact.origin_id, fact.destination_id)
        ),
        requested_mode=decision.preferred_mode if decision else None,
        selected_mode=decision.selected_mode if decision else None,
        provider_mode=decision.provider_mode if decision else None,
        mode=fact.mode,
        distance_km=fact.distance_km,
        duration_minutes=fact.duration_minutes,
        status=status,
        source=fact.provenance,
        evidence_ids=fact.evidence_ids,
        fallback_used=decision.fallback_used if decision else False,
        uncertainty=fact.duration_minutes is None,
        critical=True,
        warnings=warnings,
    )


async def _required_leg(
    resolver: RouteResolver, origin_id: str, destination_id: str, ordinal: int
) -> RouteLeg:
    return _route_leg(
        resolver, await resolver.resolve(origin_id, destination_id), ordinal
    )


async def build_route_plan(
    days: list[DayPlan], resolver: RouteResolver, input: PlannerInput
) -> tuple[TripRoutePlan, list[ValidationIssue]]:
    """Build every required edge without changing POI selection or schedules."""
    chains = []
    boundary = []
    transfers = []
    issues = []
    ordinal = 0
    for day in days:
        day_resolver = resolver.for_day(day.date) if isinstance(resolver, RouteResolver) else resolver
        if not day.start_anchor_id or not day.end_anchor_id:
            issues.append(
                ValidationIssue(
                    code="ROUTE_ANCHOR_MISSING",
                    severity="hard",
                    day_number=day.day_number,
                    message="Day route chain has no stable start/end anchor ID.",
                )
            )
            continue
        # Step 3 can add an explicit base waypoint before local sightseeing on
        # a transfer day.  Otherwise retain the frozen Step-2 activity chain.
        nodes = day.route_node_ids or [
            day.start_anchor_id,
            *[activity.poi_id for activity in day.activities],
            day.end_anchor_id,
        ]
        legs = []
        warnings = []
        if len(set(activity.poi_id for activity in day.activities)) != len(
            day.activities
        ):
            issues.append(
                ValidationIssue(
                    code="DUPLICATE_ROUTE_NODE",
                    severity="hard",
                    day_number=day.day_number,
                    message="A scheduled POI appears more than once in one route chain.",
                )
            )
        for origin, destination in (
            (a, b) for a, b in zip(nodes, nodes[1:]) if not _same_physical_node(a, b)
        ):
            ordinal += 1
            leg = await _required_leg(day_resolver, origin, destination, ordinal)
            legs.append(leg)
            if leg.uncertainty:
                warnings.extend(leg.warnings)
        expected = [
            (a, b) for a, b in zip(nodes, nodes[1:]) if not _same_physical_node(a, b)
        ]
        missing = sum(
            1
            for leg, (origin, destination) in zip(legs, expected)
            if leg.origin_node_id != origin or leg.destination_node_id != destination
        )
        missing += abs(len(legs) - len(expected))
        if missing:
            issues.append(
                ValidationIssue(
                    code="ROUTE_CHAIN_BREAK",
                    severity="hard",
                    day_number=day.day_number,
                    message="A route leg does not connect consecutive physical nodes.",
                )
            )
        chains.append(
            DayRouteChain(
                day_number=day.day_number,
                start_anchor_id=day.start_anchor_id,
                end_anchor_id=day.end_anchor_id,
                day_kind=day.day_kind,
                ordered_node_ids=nodes,
                route_legs=legs,
                structural_complete=missing == 0 and len(legs) == len(expected),
                resolved_leg_count=sum(not leg.uncertainty for leg in legs),
                unknown_leg_count=sum(leg.status == "unknown" for leg in legs),
                unsupported_leg_count=sum(leg.status == "unsupported" for leg in legs),
                missing_leg_count=missing,
                warnings=warnings,
            )
        )
    if chains:
        # These boundary legs are required even when an international plan begins
        # at its destination-side base in the timetable layer.
        if chains[0].start_anchor_id != "origin_anchor":
            ordinal += 1
            boundary.append(
                await _required_leg(
                    resolver, "origin_anchor", chains[0].start_anchor_id, ordinal
                )
            )
        if chains[-1].end_anchor_id != "end_anchor":
            ordinal += 1
            boundary.append(
                await _required_leg(
                    resolver, chains[-1].end_anchor_id, "end_anchor", ordinal
                )
            )
        for previous, current in zip(chains, chains[1:]):
            if not _same_physical_node(previous.end_anchor_id, current.start_anchor_id):
                ordinal += 1
                transfers.append(
                    await _required_leg(
                        resolver,
                        previous.end_anchor_id,
                        current.start_anchor_id,
                        ordinal,
                    )
                )
    all_legs = (
        [leg for chain in chains for leg in chain.route_legs] + boundary + transfers
    )
    plan = TripRoutePlan(
        days=chains, boundary_legs=boundary, inter_day_transfers=transfers
    )
    validation = validate_route_plan(plan, issues)
    issues[:] = validation.pop("issues")
    plan.structural_complete = validation["structural_complete"]
    plan.timing_complete = bool(all_legs) and all(
        not leg.uncertainty for leg in all_legs
    )
    plan.metrics = {
        "required_physical_legs": len(all_legs),
        "resolved_legs": sum(not leg.uncertainty for leg in all_legs),
        "unknown_legs": sum(leg.status == "unknown" for leg in all_legs),
        "unsupported_legs": sum(leg.status == "unsupported" for leg in all_legs),
        **validation,
    }
    return plan, issues


def validate_route_plan(
    plan: TripRoutePlan, initial_issues: list[ValidationIssue] | None = None
) -> dict:
    """Single source of truth for structural route validity and derived metrics."""
    hard = [issue for issue in (initial_issues or []) if issue.severity == "hard"]
    required_transfers = []
    for chain in plan.days:
        if not chain.start_anchor_id or not chain.end_anchor_id:
            hard.append(
                ValidationIssue(
                    code="ROUTE_ANCHOR_MISSING",
                    severity="hard",
                    day_number=chain.day_number,
                    message="Missing route anchor.",
                )
            )
            continue
        expected = [
            (a, b)
            for a, b in zip(chain.ordered_node_ids, chain.ordered_node_ids[1:])
            if not _same_physical_node(a, b)
        ]
        actual = [
            (leg.origin_node_id, leg.destination_node_id) for leg in chain.route_legs
        ]
        if expected != actual:
            hard.append(
                ValidationIssue(
                    code="ROUTE_CHAIN_BREAK",
                    severity="hard",
                    day_number=chain.day_number,
                    message="Route-leg endpoints do not match the ordered physical nodes.",
                )
            )
        if len(set(chain.ordered_node_ids[1:-1])) != len(chain.ordered_node_ids[1:-1]):
            hard.append(
                ValidationIssue(
                    code="DUPLICATE_ROUTE_NODE",
                    severity="hard",
                    day_number=chain.day_number,
                    message="Duplicate scheduled physical node.",
                )
            )
    for previous, current in zip(plan.days, plan.days[1:]):
        if not _same_physical_node(previous.end_anchor_id, current.start_anchor_id):
            required_transfers.append((previous.end_anchor_id, current.start_anchor_id))
    # A day that explicitly starts and ends at different anchors is also a base
    # transition. Its chain may include sightseeing nodes, so validate the full
    # connected path rather than demanding an artificial direct A→B leg.
    same_day_transfers = [
        chain
        for chain in plan.days
        if not _same_physical_node(chain.start_anchor_id, chain.end_anchor_id)
    ]
    actual_transfers = {
        (leg.origin_node_id, leg.destination_node_id)
        for leg in plan.inter_day_transfers
    }
    transfer_ok = all(pair in actual_transfers for pair in required_transfers) and all(
        bool(chain.route_legs)
        and _same_physical_node(
            chain.route_legs[0].origin_node_id, chain.start_anchor_id
        )
        and _same_physical_node(
            chain.route_legs[-1].destination_node_id, chain.end_anchor_id
        )
        for chain in same_day_transfers
    )
    if not transfer_ok:
        hard.append(
            ValidationIssue(
                code="BASE_TRANSFER_MISSING",
                severity="hard",
                message="Required inter-day base transfer is missing.",
            )
        )
    origin_ok = (
        not plan.days
        or plan.days[0].start_anchor_id == "origin_anchor"
        or any(
            leg.origin_node_id == "origin_anchor"
            and leg.destination_node_id == plan.days[0].start_anchor_id
            for leg in plan.boundary_legs
        )
    )
    end_ok = (
        not plan.days
        or plan.days[-1].end_anchor_id == "end_anchor"
        or any(
            leg.origin_node_id == plan.days[-1].end_anchor_id
            and leg.destination_node_id == "end_anchor"
            for leg in plan.boundary_legs
        )
    )
    if not origin_ok:
        hard.append(
            ValidationIssue(
                code="TRIP_ORIGIN_MISSING",
                severity="hard",
                message="Trip origin is not connected.",
            )
        )
    if not end_ok:
        hard.append(
            ValidationIssue(
                code="TRIP_END_MISSING",
                severity="hard",
                message="Trip end is not connected.",
            )
        )
    excursions = [chain for chain in plan.days if chain.day_kind == DayKind.EXCURSION]
    if not excursions:
        excursion_status = "not_applicable"
    else:
        excursion_ok = all(
            chain.start_anchor_id == chain.end_anchor_id
            and len(chain.ordered_node_ids) > 2
            and chain.route_legs
            and chain.route_legs[0].origin_node_id == chain.start_anchor_id
            and chain.route_legs[-1].destination_node_id == chain.end_anchor_id
            for chain in excursions
        )
        excursion_status = "connected" if excursion_ok else "missing"
        if not excursion_ok:
            hard.append(
                ValidationIssue(
                    code="EXCURSION_RETURN_MISSING",
                    severity="hard",
                    message="An excursion does not return to its required anchor.",
                )
            )
    missing = sum(chain.missing_leg_count for chain in plan.days) + sum(
        issue.code
        in {
            "ROUTE_CHAIN_BREAK",
            "ROUTE_ANCHOR_MISSING",
            "BASE_TRANSFER_MISSING",
            "TRIP_ORIGIN_MISSING",
            "TRIP_END_MISSING",
        }
        for issue in hard
    )
    return {
        "missing_legs": missing,
        "structural_hard_error_count": len(hard),
        "structural_complete": bool(plan.days) and not hard,
        "trip_origin_connected": origin_ok,
        "trip_end_connected": end_ok,
        "inter_day_continuity": all(
            pair in actual_transfers for pair in required_transfers
        ),
        "base_transfers_required": len(required_transfers) + len(same_day_transfers),
        "base_transfers_connected": transfer_ok,
        "excursion_returns_status": excursion_status,
        "issues": hard,
    }
