"""Planner node implementations: strategy, scheduling, review and final validation."""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
import asyncio

from ..agents import (
    LLM,
    _distance_km,
    compatible_with_day,
    feasibility_agent,
    optional_score,
    reviewer_agent,
    strategy_agent,
    validate_allocation,
)
from ..models.planning import BudgetSummary, Coordinates, DayKind, MovementClass, PlanningResult
from ..state import PipelineState
from ..tools.route_completeness import build_route_plan
from ..tools.routing import RouteResolver
from ..tools.scheduling import (
    calculate_day_utilization,
    evaluate_day,
    order_pois,
    schedule_metrics,
    validate,
)
from ..validation import final_validation


def _poi_map(world):
    return {p.id: p for p in world.input.pois}


def _cluster_map(world):
    return {
        poi_id: cluster.get("id")
        for cluster in world.input.clusters
        for poi_id in cluster.get("poi_ids", [])
    }


def _dates(start: str, count: int):
    value = date.fromisoformat(start)
    return [(value + timedelta(days=i)).isoformat() for i in range(count)]


def _is_valid_day_number(value, days) -> bool:
    return value is not None and 1 <= value <= len(days)


def _nearest_grounded_excursion_day(poi, targets, stops):
    """Choose the nearest candidate whose research base has coordinates."""
    stop_coordinates = {stop.id: stop.coordinates for stop in stops}
    located_targets = [
        pair for pair in targets if pair[0].base_stop_id in stop_coordinates
    ]
    if not located_targets:
        return None
    return min(
        located_targets,
        key=lambda pair: _distance_km(
            poi.coordinates,
            stop_coordinates[pair[0].base_stop_id],
        ),
    )[0]


def hard_issue_counter(values) -> Counter:
    """Multiset identity: duplicate same-code failures must not disappear."""
    return Counter(
        (item.code, item.day_number, tuple(getattr(item, "entity_ids", []) or []))
        for item in values
        if item.severity == "hard"
    )


def allocation_objective(days, issues, world, settings) -> dict:
    utilization = [
        calculate_day_utilization(day, world.input, settings) for day in days
    ]
    scheduled = {activity.poi_id for day in days for activity in day.activities}
    travel = sum(item["known_travel_minutes"] for item in utilization)
    slacks = [
        item["remaining_known_minutes"]
        for item in utilization
        if item["remaining_known_minutes"] is not None
    ]
    return {
        "hard_issues": sum(hard_issue_counter(issues).values()),
        "mandatory_scheduled": len(set(world.mandatory_poi_ids) & scheduled),
        "unknown_capacity_days": sum(
            item["utilization_status"] == "UNKNOWN_CAPACITY" for item in utilization
        ),
        "underutilized_known_days": sum(
            item["utilization_status"] == "UNDERUTILIZED" for item in utilization
        ),
        "slack_imbalance": (max(slacks) - min(slacks)) if slacks else 0,
        "known_travel_minutes": travel,
        "utilization": utilization,
    }


def allocation_move_improves(baseline: dict, trial: dict) -> bool:
    """A feasible LLM move needs one observable Step-4 improvement."""
    if (
        trial["hard_issues"] > baseline["hard_issues"]
        or trial["mandatory_scheduled"] < baseline["mandatory_scheduled"]
    ):
        return False
    improvements = (
        trial["underutilized_known_days"] < baseline["underutilized_known_days"]
        or trial["slack_imbalance"] < baseline["slack_imbalance"]
        or trial["known_travel_minutes"] < baseline["known_travel_minutes"]
    )
    regressions = (
        trial["unknown_capacity_days"] > baseline["unknown_capacity_days"]
        or trial["underutilized_known_days"] > baseline["underutilized_known_days"]
    )
    return improvements and not regressions


def normalize_excursion_kinds(
    strategy, mandatory_allocation: dict, original_kinds: dict[int, DayKind]
) -> list[dict]:
    """Never leave a stale excursion label behind after a reverted allocation."""
    active_days = {
        value["day_number"]
        for value in mandatory_allocation.values()
        if value.get("status")
        in {"ALLOCATED_PENDING", "ALLOCATED_UNRESOLVED", "SCHEDULED"}
    }
    repairs = []
    for day in strategy.days:
        if day.kind == DayKind.EXCURSION and day.day_number not in active_days:
            day.kind = original_kinds.get(day.day_number, DayKind.SIGHTSEEING)
            repairs.append(
                {
                    "action": "normalize_stale_excursion_kind",
                    "day_number": day.day_number,
                    "reason": "no active structured excursion allocation remains",
                }
            )
    return repairs


async def prepare(state: PipelineState):
    """Index the supplied research POIs, stops and mandatory requirements."""
    from ..adapters import build_world

    return {"world": build_world(state["input"])}


async def strategy(state: PipelineState):
    """Choose bases and day allocations using the original bounded strategy agent."""
    settings = state["settings"]
    llm = LLM(settings.reasoning_model, settings.reasoning_effort)
    try:
        value = await strategy_agent(state["world"], llm)
        metrics = {"strategy_llm": llm.metrics}
    finally:
        await llm.close()
    return {"strategy": value, "metrics": metrics}


async def _materialize(state: PipelineState, strategy):
    """Turn a proposed allocation into timed days using the shared route cache."""
    world = state["world"]
    inp = world.input
    settings = state["settings"]
    poi_map = _poi_map(world)
    resolver = RouteResolver(inp, local_attachment_km=settings.local_attachment_km)
    # Reuse evidence and explicit failures from this run. Unknown outcomes stay
    # unknown; caching them prevents identical provider calls during repair.
    shared_route_cache = state.setdefault("route_cache", {})
    resolver.cache.update(shared_route_cache)
    stay_anchors = {}
    for night, name in state.get("stay_area_by_night", {}).items():
        area = next((item for item in inp.stay_bases if item.get("name") == name), None)
        skeleton = next((item for item in strategy.days if item.day_number == night), None)
        if area and area.get("coordinates") and skeleton:
            anchor_id = f"stay:{area.get('stop_id') or name}"
            resolver.add_anchor(anchor_id, f"{name} (stay area; hotel not booked)", Coordinates.model_validate(area["coordinates"]), base_id=skeleton.base_stop_id)
            stay_anchors[night] = anchor_id
    for skeleton in strategy.days:
        if skeleton.base_stop_id and skeleton.base_stop_id.startswith("overnight:"):
            poi = poi_map.get(skeleton.base_stop_id.split(":", 1)[1])
            if poi and skeleton.base_stop_id not in resolver.nodes:
                resolver.add_anchor(
                    skeleton.base_stop_id,
                    f"Approximate overnight area near {poi.name}; property not selected.",
                    poi.coordinates,
                    base_id=poi.stop_id,
                )
    # Build the concrete, bounded day chains first, then resolve only those
    # legs once.  We never warm every directed pair across all researched POIs.
    chains: list[tuple[str, str, str]] = []
    previous_base = "origin_anchor"
    for skeleton, day_date in zip(strategy.days, _dates(inp.start_date, inp.duration_days), strict=True):
        end_id = stay_anchors.get(skeleton.day_number) or skeleton.base_stop_id or "end_anchor"
        if end_id == "origin_base":
            end_id = "origin_anchor"
        if skeleton.day_number == inp.duration_days and skeleton.kind != DayKind.ROUTE:
            end_id = "end_anchor"
        if end_id not in resolver.nodes:
            end_id = "end_anchor"
        selected = [poi_map[poi_id] for poi_id in skeleton.candidate_poi_ids if poi_id in poi_map]
        intercity = resolver.movement_class(previous_base, end_id) in {
            MovementClass.INTER_BASE, MovementClass.INTERNATIONAL,
        }
        departure = skeleton.day_number == inp.duration_days and skeleton.kind != DayKind.ROUTE
        local_start = (skeleton.base_stop_id or end_id) if (intercity and not departure) or (departure and skeleton.day_number == 1) else previous_base
        local_end = skeleton.base_stop_id or end_id if departure else end_id
        ordered = selected if skeleton.kind == DayKind.ROUTE or skeleton.preserve_order else order_pois(
            selected, resolver.node(local_start), resolver.node(local_end)
        )
        nodes = (
            [previous_base, skeleton.base_stop_id or end_id, *[poi.id for poi in ordered], end_id]
            if intercity and skeleton.kind != DayKind.ROUTE and skeleton.day_number != inp.duration_days
            else [previous_base, *[poi.id for poi in ordered], end_id]
        )
        if skeleton.day_number == inp.duration_days and skeleton.kind != DayKind.ROUTE:
            # Match the scheduler's local return to the base before leaving the city.
            nodes = [previous_base, *[poi.id for poi in ordered], skeleton.base_stop_id or end_id, end_id]
            if skeleton.day_number == 1:
                nodes.insert(1, skeleton.base_stop_id or end_id)
        for left, right in zip(nodes, nodes[1:]):
            expanded = resolver.journey_nodes(left, right)
            chains.extend((day_date, a, b) for a, b in zip(expanded, expanded[1:]) if a != b)
        previous_base = end_id
    unique_chains = list(dict.fromkeys(chains))
    semaphore = asyncio.Semaphore(6)

    async def resolve_once(pair):
        async with semaphore:
            await resolver.for_day(pair[0]).resolve(*pair[1:])

    await asyncio.gather(*(resolve_once(pair) for pair in unique_chains))
    days = []
    extra = []
    prior_end = "origin_anchor"
    for skeleton, day_date in zip(
        strategy.days, _dates(inp.start_date, inp.duration_days), strict=True
    ):
        pois = [poi_map[x] for x in skeleton.candidate_poi_ids if x in poi_map]
        end_id = stay_anchors.get(skeleton.day_number) or skeleton.base_stop_id or "end_anchor"
        if end_id.startswith("overnight:"):
            poi = poi_map[end_id.split(":", 1)[1]]
            resolver.add_anchor(
                end_id,
                f"Approximate overnight area near {poi.name}; property not selected.",
                poi.coordinates,
                base_id=poi.stop_id,
            )
        elif end_id == "origin_base":
            end_id = "origin_anchor"
        elif end_id == "end_anchor" and skeleton.day_number < inp.duration_days:
            end_id = prior_end
        if end_id not in resolver.nodes:
            end_id = "end_anchor"
        # International arrival is a real Step-2 boundary.  Keeping origin as
        # the first anchor lets Step 3 represent an unknown arrival honestly,
        # rather than pretending sightseeing starts at the destination base.
        start_id = "origin_anchor" if skeleton.day_number == 1 else prior_end
        circuit = skeleton.kind == DayKind.ROUTE
        # Do not automatically erase first/last-day POIs.  The Step-3 scheduler
        # decides whether grounded transfer timing leaves safe visit capacity.
        # The final timetable owns the physical trip departure for every trip
        # scope.  evaluate_day() safely skips this only when the final base and
        # end anchor are the same physical node.
        if skeleton.day_number == inp.duration_days and not circuit:
            end_id = "end_anchor"
        # A domestic base change is still inter-base travel.  This intentionally
        # uses structured endpoint relationships, never trip_scope or place names.
        intercity = resolver.movement_class(start_id, end_id) in {
            MovementClass.INTER_BASE,
            MovementClass.INTERNATIONAL,
        }
        # Transport class does not decide day purpose. Assigned POIs remain on a
        # route, transfer-plus-sightseeing, or ordinary sightseeing day.
        display = resolver.node(end_id).name
        day, day_issues = await evaluate_day(
            date_iso=day_date,
            day_number=skeleton.day_number,
            title=skeleton.theme,
            day_kind=DayKind.DEPARTURE if skeleton.day_number == inp.duration_days and not circuit else skeleton.kind,
            start_anchor_id=start_id,
            end_anchor_id=end_id,
            overnight_name=display,
            pois=pois,
            resolver=resolver.for_day(day_date),
            input=inp,
            local_buffer=settings.local_buffer_minutes,
            senior_rest=settings.senior_rest_minutes,
            intercity=intercity,
            transfer_buffer=settings.transfer_buffer_minutes,
            lunch_earliest=settings.lunch_earliest_minutes,
            lunch_latest=settings.lunch_latest_minutes,
            lunch_duration=settings.lunch_duration_minutes,
            max_meal_wait=settings.max_meal_wait_minutes,
            senior_rest_meal_imminent=settings.senior_rest_meal_imminent_minutes,
            enforce_optional_feasibility=True,
            preserve_order=skeleton.preserve_order,
            arrival_anchor_id=skeleton.base_stop_id if skeleton.base_stop_id in resolver.nodes else None,
            confirmation_required_poi_ids=set(skeleton.confirmation_required_poi_ids),
        )
        days.append(day)
        extra.extend(day_issues)
        prior_end = end_id
    issues = [
        *extra,
        *validate(days, inp, [poi_map[x] for x in world.mandatory_poi_ids]),
    ]
    shared_route_cache.update(resolver.cache)
    return days, issues, resolver


async def feasibility(state: PipelineState):
    """Repair allocations through the scheduler, keeping mandatory coverage first."""
    strategy = state["strategy"]
    world = state["world"]
    inp = world.input
    poi_map = _poi_map(world)
    clusters = _cluster_map(world)
    allocation_issues = validate_allocation(strategy, world)
    settings = state["settings"]
    llm = LLM(settings.reasoning_model, settings.reasoning_effort)
    try:
        actions = await feasibility_agent(strategy, world, llm)
        metric = {"feasibility_llm": llm.metrics}
    finally:
        await llm.close()
    # Step 4 uses the frozen Step-3 scheduler as its only timing oracle.  The
    # loop is deliberately finite: remove failing optionals first, then try at
    # most one well-grounded optional per genuinely usable day on each pass.
    repaired = []
    days, issues, lookup = await _materialize(state, strategy)
    original_kinds = {day.day_number: day.kind for day in strategy.days}
    scheduled_initial = {activity.poi_id for day in days for activity in day.activities}
    mandatory_allocation = {
        poi_id: {
            "status": "SCHEDULED" if poi_id in scheduled_initial else "UNALLOCATED",
            "day_number": next(
                (
                    day.day_number
                    for day in strategy.days
                    if poi_id in day.candidate_poi_ids
                ),
                None,
            ),
            "reason": None,
        }
        for poi_id in world.mandatory_poi_ids
    }
    candidate_scores = {}
    # Agent proposals are suggestions only.  Apply each one through exactly the
    # same scheduler comparison used by deterministic allocation.
    for action in actions:
        poi_id = action["poi_id"]
        source = strategy.days[action["from_day"] - 1]
        target = strategy.days[action["to_day"] - 1]
        if poi_id not in source.candidate_poi_ids or not compatible_with_day(
            poi_map[poi_id], target, world, settings.local_attachment_km
        ):
            repaired.append(
                {
                    **action,
                    "action": "reject_agent_move",
                    "reason": "source/day compatibility gate failed",
                }
            )
            continue
        source.candidate_poi_ids.remove(poi_id)
        target.candidate_poi_ids.append(poi_id)
        trial_days, trial_issues, trial_lookup = await _materialize(state, strategy)
        scheduled_trial = {a.poi_id for day in trial_days for a in day.activities}
        baseline_objective = allocation_objective(days, issues, world, settings)
        trial_objective = allocation_objective(
            trial_days, trial_issues, world, settings
        )
        if (
            poi_id in scheduled_trial
            and not (hard_issue_counter(trial_issues) - hard_issue_counter(issues))
            and allocation_move_improves(baseline_objective, trial_objective)
        ):
            days, issues, lookup = trial_days, trial_issues, trial_lookup
            repaired.append(
                {
                    **action,
                    "action": "accept_agent_move",
                    "reason": "scheduler-feasible and objectively improved allocation",
                }
            )
        else:
            target.candidate_poi_ids.remove(poi_id)
            source.candidate_poi_ids.append(poi_id)
            repaired.append(
                {
                    **action,
                    "action": "reject_agent_move",
                    "reason": "trial was infeasible, worsened hard state, or made no Step-4 objective improvement",
                }
            )
    for allocation_pass in range(settings.max_repairs):
        changed = False
        scheduled = {a.poi_id for day in days for a in day.activities}
        # Mandatory recovery is first.  If an arrival/departure day has unknown
        # timing, try another day at the POI's structured Research stop before
        # declaring the requirement unschedulable.  This preserves the base
        # strategy and never turns an attraction into an overnight base.
        for poi_id in world.mandatory_poi_ids:
            # ALLOCATED_UNRESOLVED is a terminal Step-4 state.  It has already
            # been attempted on the best excursion day and must never ping-pong.
            if poi_id in scheduled:
                mandatory_allocation[poi_id].update(status="SCHEDULED")
                continue
            if mandatory_allocation[poi_id]["status"] == "ALLOCATED_UNRESOLVED":
                continue
            source = next(
                (
                    skeleton
                    for skeleton in strategy.days
                    if poi_id in skeleton.candidate_poi_ids
                ),
                None,
            )
            targets = [
                (skeleton, day)
                for skeleton, day in zip(strategy.days, days, strict=True)
                if skeleton is not source
                and compatible_with_day(
                    poi_map[poi_id], skeleton, world, settings.local_attachment_km
                )
                and day.schedule_state == "complete"
            ]
            # A typed exact mandatory POI may be a remote day excursion.  It is
            # never an optional cross-base placement and never a new hotel.
            req_ids = set(poi_map[poi_id].requirement_ids)
            exact = any(
                req.id in req_ids
                and req.requirement_type in {"exact_poi", "poi_or_experience"}
                for req in inp.requirements
            )
            if source and not targets and exact:
                targets = [
                    (s, d)
                    for s, d in zip(strategy.days, days, strict=True)
                    if s is not source
                    and s.base_stop_id in strategy.base_sequence
                    and d.schedule_state == "complete"
                ]
            if source and targets:
                remote_excursion = exact and not any(
                    compatible_with_day(
                        poi_map[poi_id], candidate, world, settings.local_attachment_km
                    )
                    for candidate, _ in targets
                )
                if remote_excursion:
                    target = _nearest_grounded_excursion_day(
                        poi_map[poi_id], targets, inp.stops
                    )
                else:
                    target, _ = min(
                        targets, key=lambda pair: len(pair[0].candidate_poi_ids)
                    )
                if target is None:
                    repaired.append(
                        {
                            "pass": allocation_pass + 1,
                            "action": "mandatory_unresolved",
                            "poi_id": poi_id,
                            "day_number": source.day_number,
                            "reason": "no candidate excursion day has grounded base coordinates",
                        }
                    )
                    continue
                source.candidate_poi_ids.remove(poi_id)
                target.candidate_poi_ids.insert(0, poi_id)
                if remote_excursion:
                    target.kind = DayKind.EXCURSION
                mandatory_allocation[poi_id].update(
                    status="ALLOCATED_PENDING",
                    day_number=target.day_number,
                    reason=None,
                )
                repaired.append(
                    {
                        "pass": allocation_pass + 1,
                        "action": (
                            "move_mandatory_excursion"
                            if target.kind == DayKind.EXCURSION
                            else "move_mandatory"
                        ),
                        "poi_id": poi_id,
                        "from_day": source.day_number,
                        "day_number": target.day_number,
                        "reason": "mandatory moved to a timed local day or explicit selected-base excursion",
                    }
                )
                changed = True
                break
            if source:
                repaired.append(
                    {
                        "pass": allocation_pass + 1,
                        "action": "mandatory_unresolved",
                        "poi_id": poi_id,
                        "day_number": source.day_number,
                        "reason": "no compatible fully timed day exists; retained for explicit scheduler hard issue",
                    }
                )
        if changed:
            days, issues, lookup = await _materialize(state, strategy)
            scheduled_after = {
                activity.poi_id for day in days for activity in day.activities
            }
            for poi_id, allocation in mandatory_allocation.items():
                if allocation["status"] == "ALLOCATED_PENDING":
                    if poi_id in scheduled_after:
                        allocation["status"] = "SCHEDULED"
                    else:
                        allocation.update(
                            status="ALLOCATED_UNRESOLVED",
                            reason="Required excursion timing could not be grounded.",
                        )
                        repaired.append(
                            {
                                "pass": allocation_pass + 1,
                                "action": "mandatory_excursion_unresolved",
                                "poi_id": poi_id,
                                "day_number": allocation["day_number"],
                                "reason": allocation["reason"],
                            }
                        )
            continue
        # Scheduler rejection is authoritative.  A mandatory ID stays selected
        # and becomes a hard issue; an optional ID becomes traceable unscheduled.
        for skeleton, day in zip(strategy.days, days, strict=True):
            for poi_id in list(skeleton.candidate_poi_ids):
                if poi_id in world.optional_poi_ids and poi_id not in scheduled:
                    # Repair order: try every other locally compatible, timed
                    # day before declaring a Research optional unusable.
                    targets = [
                        (candidate, candidate_day)
                        for candidate, candidate_day in zip(
                            strategy.days, days, strict=True
                        )
                        if candidate is not skeleton
                        and candidate_day.schedule_state == "complete"
                        and compatible_with_day(
                            poi_map[poi_id],
                            candidate,
                            world,
                            settings.local_attachment_km,
                        )
                    ]
                    moved = False
                    for target, _ in targets:
                        skeleton.candidate_poi_ids.remove(poi_id)
                        target.candidate_poi_ids.append(poi_id)
                        trial_days, trial_issues, trial_lookup = await _materialize(
                            state, strategy
                        )
                        trial_scheduled = {
                            a.poi_id
                            for trial_day in trial_days
                            for a in trial_day.activities
                        }
                        if poi_id in trial_scheduled and not (
                            hard_issue_counter(trial_issues)
                            - hard_issue_counter(issues)
                        ):
                            days, issues, lookup = (
                                trial_days,
                                trial_issues,
                                trial_lookup,
                            )
                            changed = True
                            moved = True
                            repaired.append(
                                {
                                    "pass": allocation_pass + 1,
                                    "action": "move_optional",
                                    "poi_id": poi_id,
                                    "from_day": skeleton.day_number,
                                    "day_number": target.day_number,
                                    "reason": "scheduler found a compatible target day",
                                }
                            )
                            break
                        target.candidate_poi_ids.remove(poi_id)
                        skeleton.candidate_poi_ids.append(poi_id)
                    if moved:
                        break
                    skeleton.candidate_poi_ids.remove(poi_id)
                    changed = True
                    reason = "scheduler could not place optional candidate"
                    if day.schedule_state == "unresolved_transfer":
                        reason = "unknown critical transfer leaves no safe capacity"
                    repaired.append(
                        {
                            "pass": allocation_pass + 1,
                            "action": "reject_optional",
                            "poi_id": poi_id,
                            "day_number": skeleton.day_number,
                            "reason": reason,
                        }
                    )
            # One bounded repair per pass.  Rebuild the scheduler state before
            # considering another candidate so a pass cannot explode into a
            # large set of stale trial permutations.
            if changed:
                break
        # Overload repair moves optional candidates before dropping them.
        for issue in issues:
            if issue.code != "DAY_END_HARD_EXCEEDED" or issue.day_number is None:
                continue
            skeleton = strategy.days[issue.day_number - 1]
            optional = next(
                (
                    poi_id
                    for poi_id in reversed(skeleton.candidate_poi_ids)
                    if poi_id in world.optional_poi_ids
                ),
                None,
            )
            if optional:
                targets = [
                    (candidate, candidate_day)
                    for candidate, candidate_day in zip(
                        strategy.days, days, strict=True
                    )
                    if candidate is not skeleton
                    and candidate_day.schedule_state == "complete"
                    and compatible_with_day(
                        poi_map[optional],
                        candidate,
                        world,
                        settings.local_attachment_km,
                    )
                ]
                moved = False
                for target, _ in targets:
                    skeleton.candidate_poi_ids.remove(optional)
                    target.candidate_poi_ids.append(optional)
                    trial_days, trial_issues, trial_lookup = await _materialize(
                        state, strategy
                    )
                    if optional in {
                        activity.poi_id
                        for day in trial_days
                        for activity in day.activities
                    } and not (
                        hard_issue_counter(trial_issues) - hard_issue_counter(issues)
                    ):
                        days, issues, lookup = trial_days, trial_issues, trial_lookup
                        changed = True
                        moved = True
                        repaired.append(
                            {
                                "pass": allocation_pass + 1,
                                "action": "move_optional",
                                "poi_id": optional,
                                "from_day": skeleton.day_number,
                                "day_number": target.day_number,
                                "reason": "moved before dropping hard-overflow optional",
                            }
                        )
                        break
                    target.candidate_poi_ids.remove(optional)
                    skeleton.candidate_poi_ids.append(optional)
                if not moved:
                    skeleton.candidate_poi_ids.remove(optional)
                    changed = True
                    repaired.append(
                        {
                            "pass": allocation_pass + 1,
                            "action": "reject_optional",
                            "poi_id": optional,
                            "day_number": issue.day_number,
                            "reason": "no compatible feasible target after hard day-end failure",
                        }
                    )
        if changed:
            days, issues, lookup = await _materialize(state, strategy)
            continue
        # Only a complete timetable is usable capacity.  This prevents an
        # unknown international/intercity transfer from looking like free time.
        selected = {
            poi_id
            for skeleton in strategy.days
            for poi_id in skeleton.candidate_poi_ids
        }
        added = False
        for skeleton, day in zip(strategy.days, days, strict=True):
            utilization = calculate_day_utilization(day, inp, settings)
            if (
                utilization["utilization_status"] != "UNDERUTILIZED"
                or skeleton.kind == DayKind.ROUTE
            ):
                continue
            used_categories = {
                category.casefold()
                for poi_id in skeleton.candidate_poi_ids
                for category in [
                    *poi_map[poi_id].categories,
                    *poi_map[poi_id].normalized_categories,
                ]
            }
            used_clusters = {
                clusters.get(poi_id) for poi_id in skeleton.candidate_poi_ids
            } - {None}
            candidates = [
                poi_id
                for poi_id in world.optional_poi_ids
                if poi_id not in selected
                and compatible_with_day(
                    poi_map[poi_id], skeleton, world, settings.local_attachment_km
                )
            ]
            ranked = sorted(
                candidates,
                key=lambda poi_id: optional_score(
                    poi_map[poi_id],
                    skeleton,
                    inp.activity_interests,
                    used_categories,
                    locally_compatible=compatible_with_day(
                        poi_map[poi_id], skeleton, world, settings.local_attachment_km
                    ),
                    same_cluster=clusters.get(poi_id) in used_clusters,
                )[0],
                reverse=True,
            )
            if not ranked:
                continue
            # Static relevance is only a shortlist.  The final choice uses
            # real scheduler travel/activity deltas for at most top-N trials.
            successful = []
            for poi_id in ranked[: settings.optional_trial_top_n]:
                static_score = optional_score(
                    poi_map[poi_id],
                    skeleton,
                    inp.activity_interests,
                    used_categories,
                    locally_compatible=True,
                    same_cluster=clusters.get(poi_id) in used_clusters,
                )[0]
                skeleton.candidate_poi_ids.append(poi_id)
                trial_days, trial_issues, trial_lookup = await _materialize(
                    state, strategy
                )
                trial_scheduled = {
                    a.poi_id for trial_day in trial_days for a in trial_day.activities
                }
                trial_util = calculate_day_utilization(
                    trial_days[skeleton.day_number - 1], inp, settings
                )
                extra_travel = (
                    trial_util["known_travel_minutes"]
                    - utilization["known_travel_minutes"]
                )
                activity_minutes = next(
                    (
                        activity.planned_visit_minutes
                        for trial_day in trial_days
                        for activity in trial_day.activities
                        if activity.poi_id == poi_id
                    ),
                    None,
                )
                ratio = (
                    None
                    if not activity_minutes
                    else round(extra_travel / activity_minutes, 3)
                )
                feasible = (
                    poi_id in trial_scheduled
                    and not (
                        hard_issue_counter(trial_issues) - hard_issue_counter(issues)
                    )
                    and trial_util["remaining_known_minutes"] is not None
                    and trial_util["remaining_known_minutes"]
                    >= settings.target_end_slack_minutes
                )
                final_trial_score = (
                    static_score
                    - (ratio or 0) * 15
                    - max(
                        0,
                        (
                            settings.target_end_slack_minutes
                            - trial_util["remaining_known_minutes"]
                            if trial_util["remaining_known_minutes"] is not None
                            else 999
                        ),
                    )
                )
                candidate_scores[(skeleton.day_number, poi_id)] = {
                    "incremental_travel_minutes": extra_travel,
                    "incremental_activity_minutes": activity_minutes,
                    "travel_experience_penalty": ratio,
                    "scheduler_feasible": feasible,
                    "resulting_remaining_minutes": trial_util[
                        "remaining_known_minutes"
                    ],
                    "final_trial_score": round(final_trial_score, 3),
                }
                if feasible:
                    successful.append(
                        (
                            final_trial_score,
                            poi_id,
                            trial_days,
                            trial_issues,
                            trial_lookup,
                        )
                    )
                skeleton.candidate_poi_ids.remove(poi_id)
            if successful:
                _, poi_id, trial_days, trial_issues, trial_lookup = max(
                    successful, key=lambda item: item[0]
                )
                skeleton.candidate_poi_ids.append(poi_id)
                days, issues, lookup = trial_days, trial_issues, trial_lookup
                selected.add(poi_id)
                added = True
                repaired.append(
                    {
                        "pass": allocation_pass + 1,
                        "action": "select_optional",
                        "poi_id": poi_id,
                        "day_number": skeleton.day_number,
                        "reason": "top-N scheduler trial selected the best travel-to-experience result with target slack",
                    }
                )
        if added:
            continue
        # A single bounded rebalance can move a selected optional from a FULL
        # day into a genuinely underutilized compatible day.  It never moves a
        # mandatory POI and remains subject to scheduler and route validation.
        rebalanced = False
        utilizations = {
            day.day_number: calculate_day_utilization(day, inp, settings)
            for day in days
        }
        targets = [
            (strategy.days[day.day_number - 1], day, utilizations[day.day_number])
            for day in days
            if utilizations[day.day_number]["utilization_status"] == "UNDERUTILIZED"
        ]
        for target, target_day, target_util in targets:
            sources = sorted(
                [
                    (source, source_day, utilizations[source.day_number])
                    for source, source_day in zip(strategy.days, days, strict=True)
                    if source is not target
                    and utilizations[source.day_number]["utilization_status"] == "FULL"
                ],
                key=lambda item: item[2]["remaining_known_minutes"] or 0,
            )
            for source, source_day, source_util in sources:
                options = [
                    poi_id
                    for poi_id in source.candidate_poi_ids
                    if poi_id in world.optional_poi_ids
                    and compatible_with_day(
                        poi_map[poi_id], target, world, settings.local_attachment_km
                    )
                ]
                for poi_id in options[: settings.optional_trial_top_n]:
                    source.candidate_poi_ids.remove(poi_id)
                    target.candidate_poi_ids.append(poi_id)
                    trial_days, trial_issues, trial_lookup = await _materialize(
                        state, strategy
                    )
                    trial_target = calculate_day_utilization(
                        trial_days[target.day_number - 1], inp, settings
                    )
                    trial_source = calculate_day_utilization(
                        trial_days[source.day_number - 1], inp, settings
                    )
                    trial_route_plan, trial_route_issues = await build_route_plan(
                        trial_days, trial_lookup, inp
                    )
                    before_gap = (target_util["remaining_known_minutes"] or 0) - (
                        source_util["remaining_known_minutes"] or 0
                    )
                    after_gap = abs(
                        (trial_target["remaining_known_minutes"] or 0)
                        - (trial_source["remaining_known_minutes"] or 0)
                    )
                    feasible = (
                        poi_id
                        in {
                            activity.poi_id
                            for day in trial_days
                            for activity in day.activities
                        }
                        and not (
                            hard_issue_counter(trial_issues)
                            - hard_issue_counter(issues)
                        )
                        and trial_target["remaining_known_minutes"] is not None
                        and trial_target["remaining_known_minutes"]
                        >= settings.target_end_slack_minutes
                        and trial_route_plan.metrics.get("missing_legs", 0) == 0
                        and after_gap < abs(before_gap)
                    )
                    if feasible:
                        days, issues, lookup = (
                            trial_days,
                            [*trial_issues, *trial_route_issues],
                            trial_lookup,
                        )
                        rebalanced = True
                        repaired.append(
                            {
                                "pass": allocation_pass + 1,
                                "action": "rebalance_optional",
                                "poi_id": poi_id,
                                "from_day": source.day_number,
                                "day_number": target.day_number,
                                "reason": "moved optional from FULL day to compatible UNDERUTILIZED day after scheduler and route validation",
                            }
                        )
                        break
                    target.candidate_poi_ids.remove(poi_id)
                    source.candidate_poi_ids.append(poi_id)
                if rebalanced:
                    break
            if rebalanced:
                break
        if not rebalanced:
            break
    stale_repairs = normalize_excursion_kinds(
        strategy, mandatory_allocation, original_kinds
    )
    if stale_repairs:
        repaired.extend(stale_repairs)
        days, issues, lookup = await _materialize(state, strategy)
    allocation_issues = validate_allocation(strategy, world)
    # Keep pre-scheduler allocation errors visible after every rebuild.
    issues = [*allocation_issues, *issues]
    # An unresolved mandatory excursion is not a completed activity, but its
    # physical intent is still real: base → required POI → base.  Give Step 2
    # that explicit stable-ID chain so it emits two unknown legs rather than
    # silently collapsing the excursion to base → base.
    for poi_id, allocation in mandatory_allocation.items():
        if (
            allocation.get("status") != "ALLOCATED_UNRESOLVED"
            or allocation.get("day_number") is None
        ):
            continue
        day = days[allocation["day_number"] - 1]
        day.route_node_ids = [day.start_anchor_id, poi_id, day.end_anchor_id]
    route_plan, route_issues = await build_route_plan(days, lookup, inp)
    issues.extend(route_issues)
    scheduled = {a.poi_id for d in days for a in d.activities}
    stop_ids = {stop.id for stop in inp.stops}
    selected_base_ids = [x for x in strategy.base_sequence if x in stop_ids]
    stop_names = {stop.id: stop.name for stop in inp.stops}
    stop_names["origin_base"] = inp.origin
    selected_bases = [stop_names.get(x, x) for x in selected_base_ids]
    satisfied = []
    unsatisfied = []
    for req in world.input.requirements:
        mapped = {poi.id for poi in inp.pois if req.id in poi.requirement_ids}
        poi_match = bool(mapped) and mapped.issubset(scheduled)
        # all_route_members is fully covered only when every POI explicitly
        # mapped by Research to this requirement is scheduled.
        if req.requirement_type == "all_route_members":
            members = mapped or set(world.mandatory_poi_ids)
            poi_match = bool(members) and members.issubset(scheduled)
        base_match = req.requirement_type == "destination_or_base" and any(
            stop.id in selected_base_ids and req.id in stop.requirement_ids
            for stop in inp.stops
        )
        (satisfied if poi_match or base_match else unsatisfied).append(req.id)
    optional_unscheduled = [x for x in world.optional_poi_ids if x not in scheduled]
    budget = sum(d.known_cost for d in days)
    unknown_routes = sum(1 for d in days for leg in d.transport_legs if leg.uncertainty)
    complete = not any(i.severity == "hard" for i in issues) and not unsatisfied
    activity_ids = [activity.poi_id for day in days for activity in day.activities]
    duplicate_selected = sum(
        1 for poi_id in set(activity_ids) if activity_ids.count(poi_id) > 1
    )
    mandatory_allocated = {
        poi_id
        for skeleton in strategy.days
        for poi_id in skeleton.candidate_poi_ids
        if poi_id in world.mandatory_poi_ids
    }
    mapping_gaps = [
        req.id
        for req in inp.requirements
        if req.required
        and req.requirement_type == "destination_or_base"
        and not any(req.id in stop.requirement_ids for stop in inp.stops)
    ]
    result = PlanningResult(
        status="complete" if complete else "best_effort",
        trip_summary=f"{world.input.trip_name} — {world.input.duration_days}-Day Itinerary",
        selected_bases=selected_bases,
        nights_by_base={
            stop_names.get(key, key): value
            for key, value in strategy.nights_by_base.items()
        },
        days=days,
        unscheduled_optional_pois=optional_unscheduled,
        satisfied_mandatory_requirements=satisfied,
        unsatisfied_mandatory_requirements=unsatisfied,
        relaxed_constraints=(
            [
                "Assigned optional POIs that do not fit remain explicitly unschedulable for Step 4."
            ]
            if optional_unscheduled
            else []
        ),
        assumptions=world.input.assumptions,
        uncertainties=world.input.unresolved_gaps,
        warnings=[i.message for i in issues if i.severity != "hard"],
        budget_summary=BudgetSummary(
            known_attraction_cost=budget,
            currency=world.input.currency,
            status="partial",
            notes=[
                "Only researched adult attraction costs are included; lodging, food, and unsupported transport costs are excluded."
            ],
        ),
        planning_metrics={
            "mandatory_poi_coverage": f"{len(set(world.mandatory_poi_ids) & scheduled)}/{len(world.mandatory_poi_ids)}",
            "mandatory_pois_total": len(world.mandatory_poi_ids),
            "mandatory_pois_allocated": len(mandatory_allocated),
            "mandatory_pois_scheduled": len(set(world.mandatory_poi_ids) & scheduled),
            "mandatory_pois_unresolved": len(mandatory_allocated - scheduled),
            "mandatory_requirement_mapping_gaps": mapping_gaps,
            "mandatory_pois_selected": len(set(world.mandatory_poi_ids) & scheduled),
            "selected_optional_pois": len(set(world.optional_poi_ids) & scheduled),
            "unscheduled_optional_pois": len(optional_unscheduled),
            "duplicate_selected_pois": duplicate_selected,
            "unknown_capacity_days": sum(
                d.schedule_state == "unresolved_transfer" for d in days
            ),
            "scheduled_pois": len(scheduled),
            "unknown_route_legs": unknown_routes,
            "known_attraction_cost": budget,
            "validation_issues": len(issues),
            **schedule_metrics(days, issues),
            **route_plan.metrics,
        },
        trace_references={},
    )
    return {
        "result": result,
        "before_issues": issues,
        "feasibility_actions": [*actions, *repaired],
        "route_cache": lookup.cache,
        "route_trace": lookup.trace,
        "route_plan": route_plan,
        "mandatory_allocation": mandatory_allocation,
        "candidate_scores": candidate_scores,
        "utilization": [calculate_day_utilization(day, inp, settings) for day in days],
        "metrics": {**state.get("metrics", {}), **metric},
    }


async def review(state: PipelineState):
    """Ask the reviewer for proposals; final_safety decides whether to apply them."""
    settings = state["settings"]
    llm = LLM(settings.reviewer_model, settings.reviewer_effort)
    try:
        findings, edits = await reviewer_agent(state["result"], state["world"], llm)
        metric = {"reviewer_llm": llm.metrics}
    finally:
        await llm.close()
    return {
        "reviewer_findings": findings,
        "reviewer_edits": edits,
        "metrics": {**state.get("metrics", {}), **metric},
    }


async def final_safety(state: PipelineState):
    """Apply only reviewer edits that can be re-materialized without a hard violation."""
    import copy

    world = state["world"]
    baseline = copy.deepcopy(state["strategy"])
    result = state["result"]
    decisions = []
    applied = 0
    finding_by_id = {
        item.finding_id: item for item in state.get("reviewer_findings", [])
    }
    # This snapshot is deliberately factual, not a scorecard.  It prevents a
    # merely feasible optional move/drop from replacing the Step-4 plan.
    baseline_days, baseline_issues, _ = await _materialize(state, baseline)
    baseline_quality = allocation_objective(
        baseline_days, baseline_issues, world, state["settings"]
    )
    for edit in state.get("reviewer_edits", []):
        candidate = copy.deepcopy(baseline)
        changed = False

        finding = finding_by_id.get(edit.finding_id)
        if not finding:
            decisions.append(
                {
                    "edit": edit.model_dump(),
                    "status": "rejected",
                    "reason": "Edit is not tied to a structured reviewer finding.",
                }
            )
            continue
        # Themes are display-only; they cannot alter DayKind, bases, or routing.
        if (
            edit.action == "SET_DAY_THEME"
            and _is_valid_day_number(edit.to_day, candidate.days)
            and edit.new_theme
            and len(edit.new_theme.strip()) <= 80
        ):
            candidate.days[edit.to_day - 1].theme = edit.new_theme.strip()
            changed = True
        elif (
            edit.action == "MOVE_OPTIONAL_POI"
            and edit.poi_id
            and _is_valid_day_number(edit.from_day, candidate.days)
            and _is_valid_day_number(edit.to_day, candidate.days)
        ):
            source, target = (
                candidate.days[edit.from_day - 1],
                candidate.days[edit.to_day - 1],
            )
            if (
                edit.poi_id in world.optional_poi_ids
                and edit.poi_id in source.candidate_poi_ids
                and target.day_number != source.day_number
                and compatible_with_day(
                    _poi_map(world)[edit.poi_id],
                    target,
                    world,
                    state["settings"].local_attachment_km,
                )
            ):
                source.candidate_poi_ids.remove(edit.poi_id)
                target.candidate_poi_ids.append(edit.poi_id)
                changed = True
        elif (
            edit.action == "SWAP_POI_ORDER"
            and edit.poi_id
            and edit.other_poi_id
            and _is_valid_day_number(edit.from_day, candidate.days)
        ):
            values = candidate.days[edit.from_day - 1].candidate_poi_ids
            if edit.poi_id in values and edit.other_poi_id in values:
                a, b = values.index(edit.poi_id), values.index(edit.other_poi_id)
                values[a], values[b] = values[b], values[a]
                changed = True
        elif edit.action == "DROP_OPTIONAL_POI" and edit.poi_id:
            # The deterministic comparison below rejects a drop that merely
            # makes a known-capacity day sparse.
            for day in candidate.days:
                if (
                    edit.poi_id in day.candidate_poi_ids
                    and edit.poi_id in world.optional_poi_ids
                ):
                    day.candidate_poi_ids.remove(edit.poi_id)
                    changed = True
                    break
        if not changed:
            decisions.append(
                {
                    "edit": edit.model_dump(),
                    "status": "rejected",
                    "reason": "Unsupported or not demonstrably better without a rebuilt feasible schedule.",
                }
            )
            continue
        days, issues, _ = await _materialize(state, candidate)
        trial_quality = allocation_objective(days, issues, world, state["settings"])
        if any(issue.severity == "hard" for issue in issues):
            decisions.append(
                {
                    "edit": edit.model_dump(),
                    "status": "rejected",
                    "reason": "Re-materialized itinerary has hard validation issues.",
                }
            )
            continue
        if edit.action != "SET_DAY_THEME" and not allocation_move_improves(
            baseline_quality, trial_quality
        ):
            decisions.append(
                {
                    "edit": edit.model_dump(),
                    "status": "rejected",
                    "reason": "Safe trial did not produce a factual pacing, balance, or travel improvement.",
                }
            )
            continue
        baseline = candidate
        result.days = days
        result.planning_metrics["scheduled_pois"] = sum(
            len(day.activities) for day in days
        )
        result.planning_metrics["known_attraction_cost"] = sum(
            day.known_cost for day in days
        )
        baseline_quality = trial_quality
        applied += 1
        decisions.append(
            {
                "edit": edit.model_dump(),
                "status": "applied",
                "reason": "Rebuilt through frozen planner stages and improved the declared quality concern.",
            }
        )
    final_days, final_issues, final_resolver = await _materialize(state, baseline)
    for poi_id, allocation in state.get("mandatory_allocation", {}).items():
        if allocation.get("status") == "ALLOCATED_UNRESOLVED" and allocation.get(
            "day_number"
        ):
            day = final_days[allocation["day_number"] - 1]
            day.route_node_ids = [day.start_anchor_id, poi_id, day.end_anchor_id]
    route_plan, route_issues = await build_route_plan(
        final_days, final_resolver, state["world"].input
    )
    final_issues.extend(route_issues)
    result.days = final_days
    result.status = (
        "complete"
        if not any(issue.severity == "hard" for issue in final_issues)
        and not result.unsatisfied_mandatory_requirements
        else "best_effort"
    )
    result.trace_references = {
        "reviewer_edit_policy": "Every reviewer edit is re-materialized and must pass deterministic hard validation before application."
    }
    result.planning_metrics["reviewer_proposed_edits"] = len(
        state.get("reviewer_edits", [])
    )
    result.planning_metrics["reviewer_applied_edits"] = applied
    result.planning_metrics["reviewer_findings_count"] = len(
        state.get("reviewer_findings", [])
    )
    result.planning_metrics["reviewer_rejected_edits"] = len(decisions) - applied
    result.planning_metrics["hard_validation_issues"] = len(
        [x for x in final_issues if x.severity == "hard"]
    )
    result.planning_metrics.update(route_plan.metrics)
    validation = final_validation(
        result, world, route_plan, final_issues, state.get("mandatory_allocation", {})
    )
    # Final validation owns the delivery promise. Keep the persisted planner
    # result aligned with what the API/frontend is allowed to present.
    result.status = "complete" if validation["publishable"] else "best_effort"
    result.planning_metrics["final_validation_status"] = validation["status"]
    return {
        "result": result,
        "reviewer_decisions": decisions,
        "final_issues": final_issues,
        "route_plan": route_plan,
        "final_validation": validation,
    }
