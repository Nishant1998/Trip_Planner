"""Deterministic, grounded city-to-day skeleton built before any LLM call."""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

from ..agents import compatible_with_day, deterministic_strategy, optional_score
from ..models.planning import DayKind, DaySkeleton, TripStrategy
from ..tools.scheduling import opening_status, select_duration
from ..tools.routing import RouteResolver


def day_activity_capacity(world, day, previous_base: str) -> int:
    """Allocation allowance only; the scheduler remains the timing authority."""
    resolver = RouteResolver(world.input)
    capacity = 240 if world.input.travel_style == "relaxed" else 360
    target = day.base_stop_id or "end_anchor"
    if day.kind == DayKind.DEPARTURE:
        target = "end_anchor"
    if previous_base == target:
        return capacity
    decision = resolver.mode_resolution(previous_base, target)
    facts = [fact for (origin, destination, _), fact in resolver.cache.items()
             if origin == previous_base and destination == target
             and fact.duration_minutes is not None
             and resolver._research_decision(decision, fact) is not None]
    if not facts:
        return 0
    return max(0, capacity - max(fact.duration_minutes for fact in facts) - 30)


def build_safe_skeleton(world) -> TripStrategy:
    """Reserve boundaries, preserve base order, then allocate mandatory POIs first."""
    data = world.input
    # A thematic circuit has no city base.  The established deterministic
    # circuit partition preserves its route-member order without inventing one.
    if any(req.requirement_type == "all_route_members" for req in data.requirements):
        strategy = deterministic_strategy(world)
        for day in strategy.days:
            day.candidate_poi_ids = [
                poi_id for poi_id in day.candidate_poi_ids
                if poi_id in world.mandatory_poi_ids
            ]
        return strategy

    bases = list(world.base_stop_ids) or ["origin_base"]
    first_is_transfer = data.origin.casefold() != next(
        (stop.name.casefold() for stop in data.stops if stop.id == bases[0]), data.origin.casefold()
    )
    last_base_name = next((stop.name for stop in data.stops if stop.id == bases[-1]), data.origin)
    last_is_transfer = (data.end_location or data.origin).casefold() != last_base_name.casefold()
    reserved = int(first_is_transfer) + int(last_is_transfer) + max(0, len(bases) - 1)
    usable = max(0, data.duration_days - reserved)
    # Minimum allocation stays balanced; extra days follow actual visit demand.
    per_base = [usable // len(bases)] * len(bases)
    demand = {
        base: sum(poi.recommended_visit_minutes or poi.visit_minutes or poi.minimum_visit_minutes or 0 for poi in data.pois
                  if poi.stop_id == base)
        for base in bases
    }
    for _ in range(usable % len(bases)):
        index = max(range(len(bases)), key=lambda i: (demand[bases[i]] / (per_base[i] + 1), -per_base[i], -i))
        per_base[index] += 1
    # Rebalance only substantial unequal demand; avoid arbitrary base churn.
    for _ in range(usable):
        receiver = max(range(len(bases)), key=lambda i: demand[bases[i]] / max(1, per_base[i]))
        donors = [i for i in range(len(bases)) if per_base[i] > 1 and i != receiver]
        if not donors or demand[bases[receiver]] <= per_base[receiver] * 240:
            break
        donor = min(donors, key=lambda i: demand[bases[i]] / per_base[i])
        if demand[bases[donor]] > (per_base[donor] - 1) * 240:
            break
        per_base[donor] -= 1
        per_base[receiver] += 1

    days: list[DaySkeleton] = []
    if first_is_transfer:
        days.append(DaySkeleton(day_number=0, base_stop_id=bases[0], theme="Arrival and settle in", kind=DayKind.ARRIVAL))
    for index, base in enumerate(bases):
        for _ in range(per_base[index]):
            days.append(DaySkeleton(day_number=0, base_stop_id=base, theme="Grounded exploration", kind=DayKind.SIGHTSEEING))
        if index < len(bases) - 1:
            days.append(DaySkeleton(day_number=0, base_stop_id=bases[index + 1], theme="Transfer to next base", kind=DayKind.TRANSFER))
    if last_is_transfer:
        days.append(DaySkeleton(day_number=0, base_stop_id=bases[-1], theme="Departure and return", kind=DayKind.DEPARTURE))
    # A very short trip can leave no usable allocation slots. Keep all days
    # explicit instead of silently dropping a calendar day.
    while len(days) < data.duration_days:
        days.insert(max(0, len(days) - int(last_is_transfer)), DaySkeleton(day_number=0, base_stop_id=bases[-1], theme="Rest or buffer", kind=DayKind.REST_OR_BUFFER))
    days = days[:data.duration_days]
    for number, day in enumerate(days, 1):
        day.day_number = number
    capacities = {}
    previous = "origin_anchor"
    for day in days:
        capacities[day.day_number] = day_activity_capacity(world, day, previous)
        previous = day.base_stop_id or previous
    pois = {poi.id: poi for poi in data.pois}
    for poi_id in world.mandatory_poi_ids:
        poi = pois[poi_id]
        compatible = [day for day in days if compatible_with_day(poi, day, world)
                      and capacities[day.day_number] > 0
                      and opening_status(poi, (date.fromisoformat(data.start_date) + timedelta(days=day.day_number - 1)).isoformat(), 540, select_duration(poi))[0] != "closed"]
        target = min(compatible, key=lambda day: (sum(select_duration(pois[item]) for item in day.candidate_poi_ids) / max(1, capacities[day.day_number]), day.kind != DayKind.SIGHTSEEING)) if compatible else next((day for day in days if compatible_with_day(poi, day, world)), days[0])
        target.candidate_poi_ids.append(poi_id)
    # A provider/model outage must still leave a useful, grounded draft. Add
    # optional visits only inside the remaining planning allowance.
    used = set(world.mandatory_poi_ids)
    # Seed empty sightseeing dates before filling spare capacity on busier days.
    for day in sorted(days, key=lambda item: (bool(item.candidate_poi_ids), item.kind != DayKind.SIGHTSEEING, item.day_number)):
        remaining = capacities[day.day_number] - sum(select_duration(pois[item]) + 30 for item in day.candidate_poi_ids)
        if remaining <= 0:
            continue
        candidates = [
            pois[poi_id]
            for poi_id in world.optional_poi_ids
            if poi_id not in used and compatible_with_day(pois[poi_id], day, world)
            and opening_status(pois[poi_id], (date.fromisoformat(data.start_date) + timedelta(days=day.day_number - 1)).isoformat(), 540, select_duration(pois[poi_id]))[0] != "closed"
        ]
        future_empty = sum(1 for other in days if other is not day and not other.candidate_poi_ids
                           and other.base_stop_id == day.base_stop_id and capacities[other.day_number] > 0)
        for best in sorted(candidates, key=lambda poi: optional_score(poi, day, data.activity_interests)[0], reverse=True):
            if day.candidate_poi_ids and len([item for item in candidates if item.id not in used]) <= future_empty:
                break
            needed = select_duration(best) + 30
            if needed <= remaining:
                day.candidate_poi_ids.append(best.id)
                used.add(best.id)
                remaining -= needed
        if day.kind == DayKind.TRANSFER and day.candidate_poi_ids:
            day.kind = DayKind.TRANSFER_PLUS_SIGHTSEEING
    nights = dict(Counter(
        day.base_stop_id
        for day in days[:-1]
        if day.base_stop_id in bases
    ))
    return TripStrategy(base_sequence=bases, nights_by_base=nights, days=days, rationale="Deterministic mandatory-first safe skeleton with explicit arrival, transfer, and departure days.")
