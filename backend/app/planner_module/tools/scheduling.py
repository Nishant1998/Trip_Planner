"""Small deterministic tools. They calculate; they do not choose destinations."""

from __future__ import annotations
from datetime import datetime
from itertools import permutations
from typing import Any
from math import asin, cos, radians, sin, sqrt
from ..models.planning import (
    Activity,
    DayKind,
    DayPlan,
    PlanningPOI,
    PlannerInput,
    RouteFact,
    ScheduleBlock,
    ScheduleBlockType,
    TransportLeg,
    ValidationIssue,
)
from ..tools.routing import RouteResolver

WEEKDAYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


def minute(value: str, default: int) -> int:
    try:
        clean = value.strip()
        if clean.isdigit() and len(clean) == 4:
            return int(clean[:2]) * 60 + int(clean[2:])
        hh, mm = clean.split(":")[:2]
        return int(hh) * 60 + int(mm)
    except (AttributeError, ValueError):
        return default


def clock(value: int) -> str:
    # A day plan has no valid same-day time after midnight.  Keep an explicit
    # unknown marker instead of serializing misleading values such as 26:25.
    if value < 0 or value >= 24 * 60:
        return "unknown"
    return f"{value // 60:02d}:{value % 60:02d}"


def select_duration(poi: PlanningPOI, tight: bool = False) -> int:
    recommended = poi.recommended_visit_minutes or poi.visit_minutes
    minimum = poi.minimum_visit_minutes or recommended or 60
    if recommended is None:
        recommended = minimum
    return max(
        minimum, recommended if not tight else max(minimum, int(recommended * 0.8))
    )


def duration_note(poi: PlanningPOI, visit: int) -> tuple[str, bool]:
    """Describe whether the slot uses sourced duration evidence or an estimate."""
    sourced = poi.visit_duration_basis in {
        "official", "provider", "source_reported", "source_estimate", "corroborated_estimate",
    }
    if sourced:
        return ("Visit duration uses researched guidance.", False)
    return (
        f"Estimated {visit}-minute planning allowance; actual visit duration is unknown and must be confirmed.",
        True,
    )


def needs_session_confirmation(poi: PlanningPOI) -> bool:
    """Opening hours do not establish a class, tour, workshop or lesson session."""
    if poi.reservation_required:
        return True
    tokens = {
        token
        for category in [*poi.categories, *poi.normalized_categories]
        for token in category.casefold().replace("-", "_").split("_")
    }
    return bool(tokens & {"class", "course", "workshop", "tour", "lesson", "experience"})


def _weekday_opening_label(poi: PlanningPOI, weekday: str) -> str | None:
    for description in poi.opening_hours:
        label, separator, value = description.partition(":")
        if separator and label.strip().casefold() == weekday:
            return value.strip().casefold()
    return None


def opening_bounds(poi: PlanningPOI, date_iso: str) -> list[tuple[int, int]]:
    weekday = WEEKDAYS[datetime.fromisoformat(date_iso).weekday()]
    label = _weekday_opening_label(poi, weekday)
    if weekday in {x.casefold() for x in poi.closed_weekdays} or label == "closed":
        return []
    if label == "open 24 hours":
        return [(0, 1440)]
    # Intersect weekly provider intervals with this calendar day, including
    # the portion after midnight of a previous day's opening period.
    weekday_number = (datetime.fromisoformat(date_iso).weekday() + 1) % 7
    bounds = []
    for period in poi.opening_periods:
        open_value, close_value = period.get("open", {}), period.get("close", {})
        open_day = int(open_value.get("day", -1))
        if open_day not in range(7):
            continue
        close_day = int(close_value.get("day", open_day))
        o, c = open_value.get("time"), close_value.get("time")
        opens = (
            minute(o, 0)
            if o
            else int(open_value.get("hour", 0)) * 60 + int(open_value.get("minute", 0))
        )
        closes = (
            minute(c, 24 * 60)
            if c
            else int(close_value.get("hour", 24)) * 60
            + int(close_value.get("minute", 0))
        )
        day_span = (close_day - open_day) % 7
        if not day_span and closes < opens:
            day_span = 1
        start = open_day * 1440 + opens
        finish = (open_day + day_span) * 1440 + closes
        day_start = weekday_number * 1440
        for shift in (-7 * 1440, 0, 7 * 1440):
            left = max(0, start + shift - day_start)
            right = min(1440, finish + shift - day_start)
            if left < right:
                bounds.append((left, right))
    return sorted(set(bounds))


def opening_status(
    poi: PlanningPOI, date_iso: str, start: int, visit: int
) -> tuple[str, str | None]:
    weekday = WEEKDAYS[datetime.fromisoformat(date_iso).weekday()]
    if weekday in {x.casefold() for x in poi.closed_weekdays} or _weekday_opening_label(poi, weekday) == "closed":
        return "closed", f"{poi.name} is listed closed on {weekday.title()}."
    bounds = opening_bounds(poi, date_iso)
    if not bounds:
        return "unknown", "Opening hours not verified — confirm before visit."
    for opens, closes in bounds:
        if start >= opens and start + visit <= closes:
            return "fits_known_hours", None
    return "outside_known_hours", f"{poi.name} does not fit its known opening period."


def haversine_km(a: PlanningPOI, b: PlanningPOI) -> float:
    lat1, lon1, lat2, lon2 = map(
        radians,
        [
            a.coordinates.latitude,
            a.coordinates.longitude,
            b.coordinates.latitude,
            b.coordinates.longitude,
        ],
    )
    return (
        6371
        * 2
        * asin(
            sqrt(
                sin((lat2 - lat1) / 2) ** 2
                + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
            )
        )
    )


class RouteLookup:
    def __init__(self, input: PlannerInput):
        self.routes = {(r.origin_id, r.destination_id): r for r in input.routes}
        self.cache: dict[tuple[str, str], RouteFact] = {}

    def get(self, origin: str, destination: str) -> RouteFact:
        key = (origin, destination)
        if key in self.cache:
            return self.cache[key]
        value = self.routes.get(key) or self.routes.get((destination, origin))
        if value is None:
            value = RouteFact(
                origin_id=origin,
                destination_id=destination,
                mode="local",
                status="unknown",
                straight_line_km=0,
            )
        self.cache[key] = value
        return value


def order_pois(pois: list[PlanningPOI], start: Any = None, end: Any = None) -> list[PlanningPOI]:
    """Exact geographic ordering for <=6 POIs, otherwise nearest-neighbour. Not a travel-time claim."""
    if len(pois) <= 1:
        return pois
    def distance(left, right):
        if not getattr(left, "coordinates", None) or not getattr(right, "coordinates", None):
            return 0
        return haversine_km(left, right)

    if len(pois) <= 6:
        return list(
            min(
                permutations(pois),
                key=lambda xs: distance(start, xs[0]) + sum(
                    haversine_km(xs[i], xs[i + 1]) for i in range(len(xs) - 1)
                ) + distance(xs[-1], end),
            )
        )
    first = min(pois, key=lambda poi: distance(start, poi))
    result = [first]
    remaining = [poi for poi in pois if poi.id != first.id]
    while remaining:
        nxt = min(remaining, key=lambda p: haversine_km(result[-1], p))
        result.append(nxt)
        remaining.remove(nxt)
    return result


def build_day(
    *,
    date_iso: str,
    day_number: int,
    title: str,
    base: str | None,
    pois: list[PlanningPOI],
    route_lookup: RouteLookup,
    input: PlannerInput,
    local_buffer: int,
    senior_rest: int,
) -> DayPlan:
    """Legacy compatibility helper; production scheduling uses evaluate_day()."""
    current = minute(input.time_preferences.get("preferred_day_start"), 540)
    end_limit = minute(input.time_preferences.get("preferred_day_end"), 1200)
    plan = DayPlan(
        day_number=day_number,
        date=date_iso,
        title=title,
        overnight_base=base,
        day_start=clock(current),
        day_end=clock(current),
    )
    previous: PlanningPOI | None = None
    for poi in order_pois(pois):
        if previous:
            route = route_lookup.get(previous.id, poi.id)
            # Legacy compatibility helper: unknown travel may never become a
            # zero-minute hop.  The live scheduler below records an explicit
            # unresolved transfer instead.
            if route.duration_minutes is None:
                plan.transport_legs.append(
                    TransportLeg(
                        origin_id=previous.id,
                        destination_id=poi.id,
                        mode=route.mode,
                        start_time=clock(current),
                        end_time="unknown",
                        duration_minutes=None,
                        buffer_minutes=0,
                        distance_km=route.distance_km,
                        status=route.status,
                        evidence_ids=route.evidence_ids,
                        uncertainty=True,
                    )
                )
                plan.warnings.append(
                    f"Route time unknown for {previous.name} → {poi.name}; downstream timing unresolved."
                )
                plan.day_end = "unknown"
                return plan
            travel = route.duration_minutes
            buffer = local_buffer
            leg_start = current
            current += travel + buffer
            plan.transport_legs.append(
                TransportLeg(
                    origin_id=previous.id,
                    destination_id=poi.id,
                    mode=route.mode,
                    start_time=clock(leg_start),
                    end_time=clock(current),
                    duration_minutes=route.duration_minutes,
                    buffer_minutes=buffer,
                    distance_km=route.distance_km,
                    status=route.status,
                    evidence_ids=route.evidence_ids,
                    uncertainty=route.duration_minutes is None,
                )
            )
            if route.duration_minutes is None:
                plan.warnings.append(
                    f"Route time unknown for {previous.name} → {poi.name}; confirm locally."
                )
        duration = select_duration(poi, tight=False)
        bounds = opening_bounds(poi, date_iso)
        if bounds and current < min(x[0] for x in bounds):
            current = min(x[0] for x in bounds)
        status, warning = opening_status(poi, date_iso, current, duration)
        if status in {"closed", "outside_known_hours"}:
            plan.warnings.append(warning or f"Known hours issue for {poi.name}.")
            continue
        if current + duration > end_limit and not poi.is_mandatory:
            plan.warnings.append(
                f"Optional POI omitted from this day because it does not fit: {poi.name}."
            )
            continue
        notes = list(poi.accessibility_notes) + list(poi.restrictions)
        plan.activities.append(
            Activity(
                poi_id=poi.id,
                name=poi.name,
                start_time=clock(current),
                end_time=clock(current + duration),
                planned_visit_minutes=duration,
                mandatory=poi.is_mandatory,
                requirement_ids=poi.requirement_ids,
                address=poi.address,
                opening_hours_status=status,
                notes=notes,
                evidence_ids=poi.evidence_ids,
                image_reference=poi.image,
            )
        )
        plan.known_cost += poi.adult_entry_cost or 0
        if warning:
            plan.confirm_before_visit.append(warning)
        current += duration
        if input.travellers.get("num_seniors", 0) and len(plan.activities) % 2 == 0:
            current += senior_rest
            plan.meals_rest.append(
                f"{senior_rest}-minute rest / refreshment break included."
            )
        previous = poi
    plan.day_end = clock(current)
    return plan


async def _best_order(
    pois: list[PlanningPOI],
    start_id: str,
    end_id: str,
    resolver: RouteResolver,
    date_iso: str,
    start_minute: int,
) -> list[PlanningPOI]:
    """Choose a bounded geographic order without warming a provider matrix.

    Exact route facts are requested only for the final selected chain by
    ``evaluate_day``.  Warming every directed pair previously multiplied slow
    route-provider calls and was the main source of multi-minute runs.
    """
    del date_iso, start_minute
    return order_pois(pois, resolver.node(start_id), resolver.node(end_id))


# Step 3 canonical scheduler used by the production graph.
def _same_place(left: str, right: str) -> bool:
    return left.removeprefix("overnight:") == right.removeprefix("overnight:")


def _block(
    plan: DayPlan, kind: ScheduleBlockType, start: int, duration: int | None, **data
) -> int | None:
    """Append one visible clock event and return its known end minute."""
    end = None if duration is None else start + duration
    plan.timeline.append(
        ScheduleBlock(
            id=f"day{plan.day_number}:{len(plan.timeline) + 1}:{kind.value}",
            type=kind,
            start_time=clock(start),
            end_time=clock(end) if end is not None else "unknown",
            duration_minutes=duration,
            **data,
        )
    )
    return end


def _route_buffer(route: RouteFact, local_buffer: int, transfer_buffer: int) -> int:
    """Choose a buffer from this leg's Step-1 movement class, never its day."""
    if route.duration_minutes is None:
        return 0
    movement = (
        route.mode_resolution.movement_class.value if route.mode_resolution else "local"
    )
    if movement in {"inter_base", "international"}:
        return max(local_buffer, transfer_buffer)
    return local_buffer


def _opening_slot(
    poi: PlanningPOI, date_iso: str, arrival: int, access: int, visit: int
) -> tuple[int | None, int | None]:
    """Return an open interval that can contain access plus the full visit."""
    bounds = opening_bounds(poi, date_iso)
    if not bounds:
        return arrival, None
    for opens, closes in bounds:
        start = max(arrival, opens)
        if start + access + visit <= closes:
            return start, opens
    return None, None


def _add_meal_if_due(
    plan: DayPlan,
    current: int,
    had_activity: bool,
    earliest: int,
    latest: int,
    duration: int,
) -> int:
    """A simple midday meal policy; only insert it in the actual lunch window."""
    if not had_activity or any(
        block.type == ScheduleBlockType.MEAL for block in plan.timeline
    ):
        return current
    if earliest <= current <= latest:
        return (
            _block(
                plan,
                ScheduleBlockType.MEAL,
                current,
                duration,
                notes=[
                    f"Deterministic lunch break in configured {clock(earliest)}–{clock(latest)} window."
                ],
            )
            or current
        )
    return current


def _meal_can_replace_imminent_rest(
    plan: DayPlan, current: int, earliest: int, latest: int, imminent_minutes: int
) -> bool:
    """A nearby scheduled lunch is a valid rest; wait only for the configured short interval."""
    if any(block.type == ScheduleBlockType.MEAL for block in plan.timeline):
        return False
    return earliest <= current <= latest or 0 < earliest - current <= imminent_minutes


async def evaluate_day(
    *,
    date_iso: str,
    day_number: int,
    title: str,
    day_kind: DayKind = DayKind.SIGHTSEEING,
    start_anchor_id: str,
    end_anchor_id: str,
    overnight_name: str,
    pois: list[PlanningPOI],
    resolver: RouteResolver,
    input: PlannerInput,
    local_buffer: int,
    senior_rest: int,
    intercity: bool = False,
    transfer_buffer: int = 30,
    lunch_earliest: int = 690,
    lunch_latest: int = 840,
    lunch_duration: int = 45,
    max_meal_wait: int = 30,
    senior_rest_meal_imminent: int = 30,
    enforce_optional_feasibility: bool = False,
    preserve_order: bool = False,
    arrival_anchor_id: str | None = None,
    confirmation_required_poi_ids: set[str] | None = None,
) -> tuple[DayPlan, list[ValidationIssue]]:
    """Build one deterministic, fully explicit schedule from the Step-2 route sequence."""
    current = minute(input.time_preferences.get("preferred_day_start"), 540)
    preferred_end = minute(input.time_preferences.get("preferred_day_end"), 1200)
    plan = DayPlan(
        day_number=day_number,
        date=date_iso,
        title=title,
        day_kind=day_kind,
        overnight_base=overnight_name,
        start_anchor=resolver.node(start_anchor_id).name,
        end_anchor=resolver.node(end_anchor_id).name,
        start_anchor_id=start_anchor_id,
        end_anchor_id=end_anchor_id,
        day_start=clock(current),
        day_end=clock(current),
    )
    issues: list[ValidationIssue] = []
    ordered = (
        pois
        if day_kind == DayKind.ROUTE or preserve_order
        else await _best_order(
            pois, start_anchor_id,
            (arrival_anchor_id or end_anchor_id) if day_kind == DayKind.DEPARTURE else end_anchor_id,
            resolver, date_iso, current
        )
    )
    previous = start_anchor_id
    transfer_anchor = arrival_anchor_id or end_anchor_id
    arrival_before_departure = (
        day_kind == DayKind.DEPARTURE and day_number == 1
        and arrival_anchor_id is not None
        and not _same_place(start_anchor_id, transfer_anchor)
    )
    active_since_rest = 0
    confirmation_required_poi_ids = confirmation_required_poi_ids or set()
    routed_provisional_ids: set[str] = set()

    def note_allowance(route: RouteFact) -> bool:
        estimated = route.status == "planning_allowance_unverified"
        if estimated:
            message = "Tentative daily schedule: local travel uses estimated time allowances, not verified routes. Confirm access and timing before travel."
            if message not in plan.warnings:
                plan.warnings.append(message)
        return estimated

    def preserve_provisional(
        candidates: list[PlanningPOI], *, include_in_route_chain: bool = True
    ) -> None:
        """Retain researched choices without claiming a feasible clock time."""
        known = {item.poi_id for item in [*plan.activities, *plan.provisional_activities]}
        for poi in candidates:
            if poi.id in known:
                continue
            known.add(poi.id)
            plan.provisional_activities.append(Activity(
                poi_id=poi.id, name=poi.name, start_time="unknown", end_time="unknown",
                planned_visit_minutes=select_duration(poi), mandatory=poi.is_mandatory,
                requirement_ids=poi.requirement_ids, address=poi.address,
                opening_hours_status="requires_confirmation",
                evidence_ids=poi.evidence_ids,
                notes=["Proposed visit only: route timing and visit availability must be confirmed."],
            ))
            if include_in_route_chain:
                routed_provisional_ids.add(poi.id)
        plan.schedule_state = "partial_timing"

    def preserve_chain() -> None:
        included = {item.poi_id for item in plan.activities} | routed_provisional_ids
        nodes = [start_anchor_id]
        if arrival_before_departure or (intercity and day_kind not in {DayKind.ROUTE, DayKind.DEPARTURE}):
            nodes.append(transfer_anchor)
        nodes.extend(poi.id for poi in ordered if poi.id in included)
        if day_kind == DayKind.DEPARTURE and transfer_anchor != end_anchor_id:
            nodes.append(transfer_anchor)
        nodes.append(end_anchor_id)
        if isinstance(resolver, RouteResolver):
            expanded = [nodes[0]]
            for left, right in zip(nodes, nodes[1:]):
                expanded.extend(resolver.journey_nodes(left, right)[1:])
            nodes = expanded
        plan.route_node_ids = [node for index, node in enumerate(nodes) if index == 0 or node != nodes[index - 1]]

    def preserve_unresolved_tail() -> None:
        """Keep every later movement visible after the clock becomes unknown."""
        existing = {(leg.origin_id, leg.destination_id) for leg in plan.transport_legs}
        for left, right in zip(plan.route_node_ids, plan.route_node_ids[1:]):
            if (left, right) in existing or _same_place(left, right):
                continue
            origin_name = resolver.node(left).name
            destination_name = resolver.node(right).name
            decision = resolver.mode_resolution(left, right) if isinstance(resolver, RouteResolver) else None
            known = next((fact for fact in getattr(resolver, "cache", {}).values()
                          if fact.origin_id == left and fact.destination_id == right
                          and fact.duration_minutes is not None
                          and (fact.scheduled_service is None or
                               fact.scheduled_service.departure_at.date().isoformat() == date_iso)
                          and decision is not None
                          and resolver._research_decision(decision, fact) is not None), None)
            plan.transport_legs.append(
                TransportLeg(
                    origin_id=left,
                    destination_id=right,
                    origin_name=origin_name,
                    destination_name=destination_name,
                    mode=known.mode if known else "unknown",
                    start_time="unknown",
                    end_time="unknown",
                    duration_minutes=known.duration_minutes if known else None,
                    distance_km=known.distance_km if known else None,
                    evidence_ids=known.evidence_ids if known else [],
                    status="downstream_timing_unknown",
                    uncertainty=True,
                    provenance=known.provenance if known else "unknown",
                    mode_resolution=decision if known else None,
                    critical=True,
                )
            )
            existing.add((left, right))

    async def unresolved(
        destination_id: str, destination_name: str, route: RouteFact
    ) -> tuple[DayPlan, list[ValidationIssue]]:
        nonlocal current
        plan.transport_legs.append(
            TransportLeg(
                origin_id=previous,
                destination_id=destination_id,
                origin_name=resolver.node(previous).name,
                destination_name=destination_name,
                mode=route.mode,
                start_time=clock(current),
                end_time="unknown",
                duration_minutes=None,
                buffer_minutes=0,
                distance_km=route.distance_km,
                status=route.status,
                evidence_ids=route.evidence_ids,
                uncertainty=True,
                provenance=route.provenance,
                critical=True,
                mode_resolution=route.mode_resolution,
                scheduled_service=route.scheduled_service,
            )
        )
        _block(
            plan,
            ScheduleBlockType.UNRESOLVED_TRANSFER,
            current,
            None,
            origin_id=previous,
            destination_id=destination_id,
            grounded=False,
            status="requires_confirmation",
            notes=[
                f"Unknown transfer: {resolver.node(previous).name} → {destination_name}. Exact downstream timing intentionally not claimed."
            ],
        )
        issues.append(
            ValidationIssue(
                code="CRITICAL_ROUTE_UNKNOWN",
                severity="hard",
                day_number=day_number,
                message=f"Critical route timing unresolved: {resolver.node(previous).name} → {destination_name}.",
            )
        )
        plan.schedule_state = "unresolved_transfer"
        plan.day_end = "unknown"
        preserve_chain()
        preserve_unresolved_tail()
        return plan, issues

    async def travel_to(destination_id: str):
        """Materialize researched terminal and access segments in order."""
        nonlocal current, previous
        nodes = (resolver.journey_nodes(previous, destination_id)
                 if isinstance(resolver, RouteResolver) else [previous, destination_id])
        for target in nodes[1:]:
            route = await resolver.resolve(previous, target)
            if route.duration_minutes is None:
                preserve_provisional(ordered)
                return await unresolved(target, resolver.node(target).name, route)
            buffer = _route_buffer(route, local_buffer, transfer_buffer)
            service = (getattr(resolver.node(previous), "role", "") in {"terminal", "terminal_candidate"}
                       and getattr(resolver.node(target), "role", "") in {"terminal", "terminal_candidate"}
                       and resolver.movement_class(previous, target) != "local")
            timing = route.scheduled_service if service else None
            if timing:
                departure = timing.departure_at.hour * 60 + timing.departure_at.minute
                arrival = timing.arrival_at.hour * 60 + timing.arrival_at.minute
                if (timing.departure_at.date().isoformat() != date_iso
                        or timing.arrival_at.date().isoformat() != date_iso
                        or current + buffer > departure):
                    plan.confirm_before_visit.append("Dated service cannot fit this day's arrival/boarding window; reallocate the journey.")
                    preserve_provisional(ordered)
                    return await unresolved(target, resolver.node(target).name,
                                            route.model_copy(update={"duration_minutes": None, "status": "service_window_unresolved"}))
                wait = departure - buffer - current
                if wait:
                    current = _block(plan, ScheduleBlockType.WAIT, current, wait,
                                     notes=["Wait for the evidenced service departure."]) or current
            if service and buffer:
                current = _block(plan, ScheduleBlockType.BUFFER, current, buffer,
                                 origin_id=previous, destination_id=target,
                                 notes=["Provisional pre-boarding allowance; confirm operator requirements."]) or current
            travel_start = current
            allowance = note_allowance(route)
            current = _block(plan, ScheduleBlockType.TRAVEL, current, route.duration_minutes,
                             origin_id=previous, destination_id=target,
                             grounded=not allowance,
                             notes=["Estimated local travel allowance; confirm route."] if allowance else []) or current
            if timing:
                current = arrival
                block = plan.timeline[-1]
                block.end_time = clock(current)
                block.scheduled_service = timing
                block.notes.append(f"Local service times: {timing.departure_at.isoformat()} to {timing.arrival_at.isoformat()}.")
            if buffer and not service:
                current = _block(plan, ScheduleBlockType.BUFFER, current, buffer,
                                 origin_id=previous, destination_id=target,
                                 notes=["Planning arrival/transfer buffer."]) or current
            plan.transport_legs.append(TransportLeg(
                origin_id=previous, destination_id=target,
                origin_name=resolver.node(previous).name, destination_name=resolver.node(target).name,
                mode=route.mode, start_time=clock(travel_start), end_time=clock(current),
                duration_minutes=route.duration_minutes, buffer_minutes=0 if service else buffer,
                distance_km=route.distance_km, status=route.status,
                provenance=route.provenance, evidence_ids=route.evidence_ids,
                mode_resolution=route.mode_resolution, critical=True,
                uncertainty=allowance or service and (timing is None or any(
                    getattr(resolver.node(node), "role", "") == "terminal_candidate" for node in (previous, target))),
                scheduled_service=timing,
            ))
            if service and (timing is None or plan.transport_legs[-1].uncertainty):
                plan.confirm_before_visit.append("Terminal identity, service departure and local timezone timing require confirmation.")
            previous = target
        return None

    # A transfer-plus-sightseeing day first reaches the new base.  This keeps
    # the researched inter-base leg explicit and prevents a false direct jump
    # from the old base to the first attraction in the new city.
    if (
        arrival_before_departure or (
            intercity
            and day_kind not in {DayKind.ROUTE, DayKind.DEPARTURE}
            and not _same_place(start_anchor_id, transfer_anchor)
        )
    ):
        plan.route_node_ids = [start_anchor_id, transfer_anchor]
        stopped = await travel_to(transfer_anchor)
        if stopped:
            return stopped
        if day_kind != DayKind.ROUTE and not preserve_order:
            ordered = await _best_order(
                pois, transfer_anchor,
                transfer_anchor if day_kind == DayKind.DEPARTURE else end_anchor_id,
                resolver, date_iso, current
            )

    for poi_index, poi in enumerate(ordered):
        if poi.id in confirmation_required_poi_ids:
            preserve_provisional([poi], include_in_route_chain=False)
            plan.warnings.append(
                f"{poi.name} requires a confirmed bookable session; general opening hours are not a session time."
            )
            continue
        before_meal = current
        current = _add_meal_if_due(
            plan,
            current,
            bool(plan.activities),
            lunch_earliest,
            lunch_latest,
            lunch_duration,
        )
        if current != before_meal:
            active_since_rest = 0
        route = await resolver.resolve(previous, poi.id)
        if route.duration_minutes is None:
            if enforce_optional_feasibility and not poi.is_mandatory:
                preserve_provisional([poi])
                plan.warnings.append(
                    f"Provisional visit, timing to confirm: {poi.name}."
                )
                continue
            preserve_provisional(ordered[poi_index:])
            return await unresolved(poi.id, poi.name, route)
        buffer = _route_buffer(route, local_buffer, transfer_buffer)
        travel_start = current
        arrival = current + route.duration_minutes + buffer
        visit = select_duration(poi)
        access = poi.queue_or_access_minutes or 0
        # A lunch wait is acceptable only when lunch is genuinely imminent.
        # Never create an unexplained multi-hour idle period before a route.
        meal_window_conflict = (
            bool(plan.activities)
            and not any(block.type == ScheduleBlockType.MEAL for block in plan.timeline)
            and current < lunch_earliest
            and arrival + access + visit > lunch_latest
        )
        if meal_window_conflict and lunch_earliest - current <= max_meal_wait:
            current = (
                _block(
                    plan,
                    ScheduleBlockType.WAIT,
                    current,
                    lunch_earliest - current,
                    notes=[
                        "Brief wait for the configured lunch window before a long uninterrupted segment."
                    ],
                )
                or current
            )
            current = (
                _block(
                    plan,
                    ScheduleBlockType.MEAL,
                    current,
                    lunch_duration,
                    notes=[
                        "Deterministic lunch before a long route/activity sequence."
                    ],
                )
                or current
            )
            active_since_rest = 0
            travel_start = current
            arrival = current + route.duration_minutes + buffer
            meal_window_conflict = False
        status, warning = opening_status(poi, date_iso, arrival, access + visit)
        if status == "closed":
            message = warning or f"Known closed day: {poi.name}."
            issues.append(
                ValidationIssue(
                    code=(
                        "MANDATORY_UNSCHEDULABLE_ASSIGNED_POI"
                        if poi.is_mandatory
                        else "UNSCHEDULABLE_ASSIGNED_POI"
                    ),
                    severity="hard" if poi.is_mandatory else "warning",
                    day_number=day_number,
                    message=message,
                )
            )
            continue
        slot, opening = _opening_slot(poi, date_iso, arrival, access, visit)
        if slot is None:
            message = f"{poi.name} cannot fit its researched visit duration inside known opening hours."
            issues.append(
                ValidationIssue(
                    code=(
                        "MANDATORY_UNSCHEDULABLE_ASSIGNED_POI"
                        if poi.is_mandatory
                        else "UNSCHEDULABLE_ASSIGNED_POI"
                    ),
                    severity="hard" if poi.is_mandatory else "warning",
                    day_number=day_number,
                    message=message,
                )
            )
            continue
        # Optional content is admitted only after a complete conservative trial
        # (arrival, visit, buffer and return to the day anchor) fits. This is
        # deliberately before writing timeline blocks so it cannot create an
        # infeasible day that validation must repair later.
        if enforce_optional_feasibility and not poi.is_mandatory:
            return_nodes = [poi.id, end_anchor_id]
            if day_kind == DayKind.DEPARTURE and transfer_anchor != end_anchor_id:
                return_nodes = [poi.id, transfer_anchor, end_anchor_id]
            return_routes = [await resolver.resolve(left, right) for left, right in zip(return_nodes, return_nodes[1:])]
            conservative_end = (
                slot + access + visit
                + sum((item.duration_minutes or 0) + _route_buffer(item, local_buffer, transfer_buffer) for item in return_routes)
                + (lunch_duration if not any(block.type == ScheduleBlockType.MEAL for block in plan.timeline) and slot <= lunch_latest else 0)
            )
            hard_end = input.time_preferences.get("hard_day_end")
            hard_end_minute = minute(hard_end, 24 * 60) if hard_end else 24 * 60
            if (
                any(item.duration_minutes is None for item in return_routes)
                or conservative_end > preferred_end
                or conservative_end > hard_end_minute
                or conservative_end >= 24 * 60
            ):
                plan.warnings.append(
                    f"Optional POI omitted because the complete day would not fit safely: {poi.name}."
                )
                continue
        allowance = note_allowance(route)
        current = (
            _block(
                plan,
                ScheduleBlockType.TRAVEL,
                current,
                route.duration_minutes,
                origin_id=previous,
                destination_id=poi.id,
                grounded=not allowance,
                notes=["Estimated local travel allowance; confirm route." if allowance else f"Grounded {route.mode} route."],
            )
            or current
        )
        active_since_rest += route.duration_minutes
        if buffer:
            current = (
                _block(
                    plan,
                    ScheduleBlockType.BUFFER,
                    current,
                    buffer,
                    origin_id=previous,
                    destination_id=poi.id,
                    notes=["Planning arrival/transfer buffer."],
                )
                or current
            )
            active_since_rest += buffer
        plan.transport_legs.append(
            TransportLeg(
                origin_id=previous,
                destination_id=poi.id,
                origin_name=resolver.node(previous).name,
                destination_name=poi.name,
                mode=route.mode,
                start_time=clock(travel_start),
                end_time=clock(current),
                duration_minutes=route.duration_minutes,
                buffer_minutes=buffer,
                distance_km=route.distance_km,
                status=route.status,
                evidence_ids=route.evidence_ids,
                uncertainty=allowance,
                provenance=route.provenance,
                critical=True,
                mode_resolution=route.mode_resolution,
            )
        )
        if opening is not None and current < slot:
            current = (
                _block(
                    plan,
                    ScheduleBlockType.WAIT,
                    current,
                    slot - current,
                    poi_id=poi.id,
                    notes=[f"Waiting for known opening time {clock(opening)}."],
                )
                or current
            )
        # The pre-wait arrival can be outside known hours.  Store the status of
        # the actual scheduled access/visit slot instead.
        status, warning = opening_status(poi, date_iso, current, access + visit)
        if status == "unknown":
            plan.confirm_before_visit.append(
                warning or "Opening hours not verified — confirm before visit."
            )
        if access:
            current = (
                _block(
                    plan,
                    ScheduleBlockType.ACCESS,
                    current,
                    access,
                    poi_id=poi.id,
                    notes=["Researched queue/access allowance."],
                )
                or current
            )
            active_since_rest += access
        activity_start = current
        visit_note, duration_is_estimated = duration_note(poi, visit)
        if duration_is_estimated:
            plan.confirm_before_visit.append(f"{poi.name}: {visit_note}")
        current = (
            _block(
                plan,
                ScheduleBlockType.ACTIVITY,
                current,
                visit,
                poi_id=poi.id,
                notes=[visit_note],
            )
            or current
        )
        active_since_rest += visit
        plan.activities.append(
            Activity(
                poi_id=poi.id,
                name=poi.name,
                start_time=clock(activity_start),
                end_time=clock(current),
                planned_visit_minutes=visit,
                mandatory=poi.is_mandatory,
                requirement_ids=poi.requirement_ids,
                address=poi.address,
                opening_hours_status=status,
                notes=[*poi.accessibility_notes, *poi.restrictions, visit_note],
                evidence_ids=poi.evidence_ids,
                image_reference=poi.image,
            )
        )
        if needs_session_confirmation(poi):
            activity = plan.activities[-1]
            activity.start_time = activity.end_time = "unknown"
            activity.opening_hours_status = "requires_confirmation"
            activity.notes.append("Session availability is not evidenced; this planned slot is not a confirmed booking.")
            plan.confirm_before_visit.append(
                f"{poi.name}: confirm a session before relying on a specific visit time."
            )
            plan.provisional_activities.append(activity)
            plan.activities.remove(activity)
            plan.schedule_state = "partial_timing"
        plan.known_cost += poi.adult_entry_cost or 0
        previous = poi.id
        if meal_window_conflict:
            issues.append(
                ValidationIssue(
                    code="MEAL_WINDOW_CONFLICT",
                    severity="warning",
                    day_number=day_number,
                    message="Lunch could not be placed without an artificial long wait or an unsupported stop inside a continuous route.",
                )
            )
        before_meal = current
        current = _add_meal_if_due(
            plan, current, True, lunch_earliest, lunch_latest, lunch_duration
        )
        if current != before_meal:
            active_since_rest = 0
        if (
            input.travellers.get("num_seniors", 0)
            and senior_rest
            and active_since_rest >= 180
        ):
            if _meal_can_replace_imminent_rest(
                plan, current, lunch_earliest, lunch_latest, senior_rest_meal_imminent
            ):
                if current < lunch_earliest:
                    current = (
                        _block(
                            plan,
                            ScheduleBlockType.WAIT,
                            current,
                            lunch_earliest - current,
                            notes=[
                                "Waiting briefly for the configured lunch window; lunch satisfies the senior rest need."
                            ],
                        )
                        or current
                    )
                current = _add_meal_if_due(
                    plan, current, True, lunch_earliest, lunch_latest, lunch_duration
                )
                active_since_rest = 0
            else:
                current = (
                    _block(
                        plan,
                        ScheduleBlockType.REST,
                        current,
                        senior_rest,
                        notes=["Senior-friendly rest after sustained travel/activity."],
                    )
                    or current
                )
                active_since_rest = 0

    before_meal = current
    current = _add_meal_if_due(
        plan,
        current,
        bool(plan.activities),
        lunch_earliest,
        lunch_latest,
        lunch_duration,
    )
    if current != before_meal:
        active_since_rest = 0
    if day_kind == DayKind.DEPARTURE and transfer_anchor != end_anchor_id and not _same_place(previous, transfer_anchor):
        stopped = await travel_to(transfer_anchor)
        if stopped:
            return stopped
    if not _same_place(previous, end_anchor_id):
        stopped = await travel_to(end_anchor_id)
        if stopped:
            return stopped
    if (
        plan.schedule_state == "complete"
        and plan.timeline
        and current < preferred_end
        and preferred_end - current >= 60
    ):
        current = (
            _block(
                plan,
                ScheduleBlockType.FREE_TIME,
                current,
                preferred_end - current,
                notes=["Intentional free time for rest, flexible meals or hotel time; no additional activity is claimed."],
            )
            or current
        )
    plan.day_end = clock(current)
    if plan.provisional_activities:
        plan.day_end = "unknown"
        preserve_chain()
        preserve_unresolved_tail()
    elif plan.route_node_ids or day_kind == DayKind.DEPARTURE:
        preserve_chain()
    if current >= 24 * 60:
        issues.append(
            ValidationIssue(
                code="DAY_ROLLOVER",
                severity="hard",
                day_number=day_number,
                message="The day would continue past midnight; move or remove activities.",
            )
        )
    if current > preferred_end:
        overrun = current - preferred_end
        issues.append(
            ValidationIssue(
                code="DAY_END_SOFT_EXCEEDED",
                severity="warning",
                day_number=day_number,
                message=f"Preferred day end exceeded by {overrun} minutes.",
            )
        )
        if overrun > 30:
            issues.append(
                ValidationIssue(
                    code="DAY_DURATION_UNREALISTIC",
                    severity="hard",
                    day_number=day_number,
                    message="The day exceeds the preferred end by more than 30 minutes.",
                )
            )
    hard_end = input.time_preferences.get("hard_day_end")
    if hard_end and current > minute(hard_end, 24 * 60):
        issues.append(
            ValidationIssue(
                code="DAY_END_HARD_EXCEEDED",
                severity="hard",
                day_number=day_number,
                message="Configured hard day-end limit exceeded.",
            )
        )
    defer_uncertain_service_timing(plan)
    return plan, issues


def defer_uncertain_service_timing(plan: DayPlan) -> None:
    """Keep route durations, but do not publish clocks dependent on an unknown service."""
    first = next((leg for leg in plan.transport_legs
                  if leg.uncertainty and leg.duration_minutes is not None
                  and leg.status != "planning_allowance_unverified"), None)
    if first is None:
        return
    affected = False
    deferred_pois = set()
    for block in plan.timeline:
        if (block.type == ScheduleBlockType.TRAVEL and block.origin_id == first.origin_id
                and block.destination_id == first.destination_id):
            affected = True
        if affected:
            block.start_time = block.end_time = "unknown"
            block.status = "requires_confirmation"
            if block.poi_id:
                deferred_pois.add(block.poi_id)
    affected = False
    for leg in plan.transport_legs:
        affected = affected or leg is first
        if affected:
            leg.start_time = leg.end_time = "unknown"
            leg.uncertainty = True
    for activity in list(plan.activities):
        if activity.poi_id in deferred_pois:
            activity.start_time = activity.end_time = "unknown"
            activity.opening_hours_status = "requires_confirmation"
            activity.notes.append("Visit timing depends on an unconfirmed service arrival.")
            plan.provisional_activities.append(activity)
            plan.activities.remove(activity)
    plan.schedule_state = "partial_timing"
    plan.day_end = "unknown"


def validate(
    days: list[DayPlan], input: PlannerInput, mandatory_pois: list[PlanningPOI]
) -> list[ValidationIssue]:
    """Validate every visible timeline minute, not just activity timestamps."""
    issues: list[ValidationIssue] = []
    seen = set()
    poi_by_id = {poi.id: poi for poi in input.pois}
    for day in days:
        expected = minute(day.day_start, 0)
        unresolved = False
        service_deferred = False
        for block in day.timeline:
            service_deferred = service_deferred or any(
                leg.uncertainty and leg.duration_minutes is not None
                and leg.status != "planning_allowance_unverified"
                and block.type == ScheduleBlockType.TRAVEL
                and block.origin_id == leg.origin_id and block.destination_id == leg.destination_id
                for leg in day.transport_legs
            )
            if (service_deferred and block.status == "requires_confirmation"
                    and block.start_time == block.end_time == "unknown"):
                if block.duration_minutes is not None and block.duration_minutes < 0:
                    issues.append(ValidationIssue(code="INVALID_TIMELINE_DURATION", severity="hard",
                                                  day_number=day.day_number, message="Negative provisional duration."))
                unresolved = True
                continue
            start = minute(block.start_time, -1)
            if start != expected:
                issues.append(
                    ValidationIssue(
                        code="TIME_OVERLAP"
                        if start < expected
                        else "HIDDEN_TIMELINE_GAP",
                        severity="hard",
                        day_number=day.day_number,
                        message=f"Timeline block {block.id} is not continuous.",
                    )
                )
            if block.duration_minutes is None:
                if block.type != ScheduleBlockType.UNRESOLVED_TRANSFER:
                    issues.append(
                        ValidationIssue(
                            code="UNKNOWN_TIMELINE_DURATION",
                            severity="hard",
                            day_number=day.day_number,
                            message=f"Unexpected unknown duration in {block.id}.",
                        )
                    )
                unresolved = True
                continue
            end = minute(block.end_time, -1)
            elapsed = block.scheduled_service.elapsed_minutes if block.scheduled_service else end - start
            if block.duration_minutes < 0 or elapsed != block.duration_minutes:
                issues.append(
                    ValidationIssue(
                        code="INVALID_TIMELINE_DURATION",
                        severity="hard",
                        day_number=day.day_number,
                        message=f"Invalid duration in {block.id}.",
                    )
                )
            expected = end
            if unresolved:
                issues.append(
                    ValidationIssue(
                        code="TIMING_AFTER_UNKNOWN_TRANSFER",
                        severity="hard",
                        day_number=day.day_number,
                        message="Exact timing appears after an unresolved transfer.",
                    )
                )
            if block.type == ScheduleBlockType.ACTIVITY and block.poi_id:
                seen.add(block.poi_id)
                poi = poi_by_id.get(block.poi_id)
                if poi and block.duration_minutes < (poi.minimum_visit_minutes or 0):
                    issues.append(
                        ValidationIssue(
                            code="VISIT_BELOW_MINIMUM",
                            severity="hard",
                            day_number=day.day_number,
                            message=f"Visit below researched minimum: {poi.name}.",
                        )
                    )
                if (
                    poi
                    and poi.maximum_visit_minutes
                    and block.duration_minutes > poi.maximum_visit_minutes
                ):
                    issues.append(
                        ValidationIssue(
                            code="VISIT_ABOVE_MAXIMUM",
                            severity="hard",
                            day_number=day.day_number,
                            message=f"Visit above researched maximum: {poi.name}.",
                        )
                    )
        if day.schedule_state == "complete" and minute(day.day_end, -1) != expected:
            issues.append(
                ValidationIssue(
                    code="DAY_END_TIMELINE_MISMATCH",
                    severity="hard",
                    day_number=day.day_number,
                    message="Day end does not equal final timeline event.",
                )
            )
        if day.schedule_state != "complete" and not service_deferred and not day.provisional_activities and not any(
            b.type == ScheduleBlockType.UNRESOLVED_TRANSFER for b in day.timeline
        ):
            issues.append(
                ValidationIssue(
                    code="UNRESOLVED_STATE_WITHOUT_BLOCK",
                    severity="hard",
                    day_number=day.day_number,
                    message="Unresolved day lacks an explicit transfer block.",
                )
            )
        for leg in day.transport_legs:
            matches = [
                b
                for b in day.timeline
                if b.type == ScheduleBlockType.TRAVEL
                and b.origin_id == leg.origin_id
                and b.destination_id == leg.destination_id
            ]
            if (not matches and leg.status == "downstream_timing_unknown"
                    and leg.uncertainty and leg.start_time == leg.end_time == "unknown"):
                # A retained route duration is not a scheduled timeline interval.
                continue
            if leg.duration_minutes is not None and (
                not matches or matches[0].duration_minutes != leg.duration_minutes
            ):
                issues.append(
                    ValidationIssue(
                        code="ROUTE_DURATION_MISMATCH",
                        severity="hard",
                        day_number=day.day_number,
                        message=f"Timeline differs from grounded route: {leg.origin_id} → {leg.destination_id}.",
                    )
                )
    for poi in mandatory_pois:
        if poi.id not in seen:
            issues.append(
                ValidationIssue(
                    code="MISSING_MANDATORY_POI",
                    severity="hard",
                    message=f"Mandatory POI not scheduled: {poi.name}",
                )
            )
    return issues


def schedule_metrics(days: list[DayPlan], issues: list[ValidationIssue]) -> dict:
    blocks = [block for day in days for block in day.timeline]
    return {
        "days_total": len(days),
        "days_fully_timed": sum(day.schedule_state == "complete" for day in days),
        "days_partially_timed": sum(day.schedule_state != "complete" for day in days),
        "resolved_timeline_minutes": sum(
            block.duration_minutes or 0 for block in blocks
        ),
        "unknown_transfer_count": sum(
            block.type == ScheduleBlockType.UNRESOLVED_TRANSFER for block in blocks
        ),
        "opening_hour_conflicts": sum(
            issue.code
            in {
                "OPENING_HOURS",
                "UNSCHEDULABLE_ASSIGNED_POI",
                "MANDATORY_UNSCHEDULABLE_ASSIGNED_POI",
            }
            for issue in issues
        ),
        "time_overlap_count": sum(issue.code == "TIME_OVERLAP" for issue in issues),
        "hidden_gap_count": sum(
            issue.code == "HIDDEN_TIMELINE_GAP" for issue in issues
        ),
        "day_end_violations": sum(
            issue.code.startswith("DAY_END_") for issue in issues
        ),
        "mandatory_unschedulable_count": sum(
            issue.code == "MANDATORY_UNSCHEDULABLE_ASSIGNED_POI" for issue in issues
        ),
        "optional_unschedulable_count": sum(
            issue.code == "UNSCHEDULABLE_ASSIGNED_POI" for issue in issues
        ),
        "duration_relaxation_count": 0,
        "meal_blocks": sum(block.type == ScheduleBlockType.MEAL for block in blocks),
        "rest_blocks": sum(block.type == ScheduleBlockType.REST for block in blocks),
    }


def calculate_day_utilization(
    day: DayPlan, input: PlannerInput, settings: Any
) -> dict[str, Any]:
    """Canonical Step-4 capacity signal used for both decisions and debug output."""
    preferred_end = minute(input.time_preferences.get("preferred_day_end"), 20 * 60)
    known_end = (
        None
        if day.schedule_state != "complete" or day.day_end == "unknown"
        else minute(day.day_end, preferred_end)
    )
    blocks = day.timeline

    def values(kind):
        return sum(
            block.duration_minutes or 0 for block in blocks if block.type.value == kind
        )

    activity = sum(activity.planned_visit_minutes for activity in day.activities)
    remaining = None if known_end is None else max(0, preferred_end - known_end)
    slack = settings.target_end_slack_minutes
    status = (
        "UNKNOWN_CAPACITY"
        if remaining is None
        else (
            "UNDERUTILIZED"
            if remaining >= slack * 2
            else "HEALTHY"
            if remaining >= slack
            else "FULL"
        )
    )
    return {
        "day_number": day.day_number,
        "schedule_state": day.schedule_state,
        "day_start_minutes": None
        if day.day_start == "unknown"
        else minute(day.day_start, 0),
        "preferred_day_end_minutes": preferred_end,
        "known_window_minutes": (
            None
            if known_end is None
            else max(0, preferred_end - minute(day.day_start, 0))
        ),
        "known_timeline_end_minutes": known_end,
        "activity_minutes": activity,
        "known_travel_minutes": values("travel"),
        "buffer_minutes": values("buffer"),
        "meal_rest_minutes": values("meal") + values("rest"),
        "wait_minutes": values("wait"),
        "remaining_known_minutes": remaining,
        "utilization_status": status,
    }
