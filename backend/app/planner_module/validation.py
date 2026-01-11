"""Final factual validation; deliberately not a quality scorecard."""

from __future__ import annotations
from datetime import date, timedelta
from .tools.transport import is_terminal_stop


def has_day_content(day):
    """Visits or actual movement count; meals and zero-length hops do not."""
    return bool(day.activities or day.provisional_activities) or any(
        getattr(leg, "origin_id", None) != getattr(leg, "destination_id", None)
        and (leg.duration_minutes is None or leg.duration_minutes > 0)
        for leg in day.transport_legs
    )


def terminal_connection_gaps(result, world):
    """City-level continuity does not prove access to a scheduled-service terminal."""
    terminals = {
        stop.id for stop in getattr(world.input, "stops", [])
        if is_terminal_stop(stop)
    }
    gaps = []
    for day in result.days:
        for leg in day.transport_legs:
            mode = getattr(leg, "mode", "")
            decision = getattr(leg, "mode_resolution", None)
            movement = getattr(decision, "movement_class", None)
            major_service = mode == "flight" or (
                mode in {"train", "transit", "bus", "ferry"}
                and movement in {"international", "inter_base", "route_stage"}
            )
            if major_service and not {leg.origin_id, leg.destination_id} <= terminals:
                gaps.append(f"Day {day.day_number}: {mode} terminal endpoints and access need confirmation.")
    return list(dict.fromkeys(gaps))


def final_validation(result, world, route_plan, issues, mandatory_allocation):
    scheduled = {a.poi_id for day in result.days for a in day.activities}
    research = {poi.id for poi in world.input.pois}
    failures = []
    deadline_draft = bool(getattr(route_plan, "metrics", {}).get("deadline_draft"))
    expected_dates = [
        (date.fromisoformat(world.input.start_date) + timedelta(days=index)).isoformat()
        for index in range(world.input.duration_days)
    ]
    if [day.date for day in result.days] != expected_dates:
        failures.append("Calendar days are missing, duplicated or out of order.")
    blank_days = [day.day_number for day in result.days
                  if day.day_kind.value != "rest_or_buffer"
                  and not has_day_content(day)]
    if blank_days:
        failures.append(f"Unexplained blank days: {blank_days}.")
    for previous, following in zip(result.days, result.days[1:]):
        if getattr(previous, "end_anchor_id", None) and getattr(following, "start_anchor_id", None) and previous.end_anchor_id != following.start_anchor_id:
            failures.append(f"Traveller continuity broken between days {previous.day_number} and {following.day_number}.")
    for day in result.days:
        if day.day_kind.value == "rest_or_buffer" and not day.activities and not day.transport_legs and not getattr(day, "warnings", []):
            failures.append(f"Rest day {day.day_number} has no explanation.")
    allocated = {
        poi_id
        for day in getattr(result, "days", [])
        for poi_id in getattr(day, "route_node_ids", [])
        if poi_id in research
    }
    if not scheduled <= research:
        failures.append("Scheduled POI is outside Research.")
    provisional = {activity.poi_id for day in result.days for activity in day.provisional_activities}
    if not provisional <= research:
        failures.append("Provisional POI is outside Research.")
    if not allocated <= research:
        failures.append("Allocated POI is outside Research.")
    if len(scheduled) != sum(len(day.activities) for day in result.days):
        failures.append("A POI is scheduled more than once.")
    budget = getattr(world.input, "budget", {}) or {}
    budget_limit = budget.get("total") if isinstance(budget, dict) else None
    summary = getattr(result, "budget_summary", None)
    subtotal = (
        (getattr(summary, "total_verified", 0) or 0)
        + (getattr(summary, "total_estimated", 0) or 0)
    )
    if isinstance(budget_limit, (int, float)) and subtotal > budget_limit:
        failures.append("Known and estimated cost subtotal already exceeds the trip budget.")
    if not deadline_draft and route_plan.metrics.get(
        "missing_legs", result.planning_metrics.get("missing_legs", 0)
    ):
        failures.append("Required physical route leg is missing.")
    if not deadline_draft and not route_plan.structural_complete:
        failures.append("Route structure is incomplete.")
    for poi_id, state in mandatory_allocation.items():
        if state.get("status") == "ALLOCATED_UNRESOLVED" and poi_id in scheduled:
            failures.append("Unresolved mandatory POI is incorrectly scheduled.")
        if state.get("status") == "SCHEDULED" and poi_id not in scheduled:
            failures.append(
                "Mandatory allocation says scheduled but the POI is absent."
            )
    # Unknown is permissible only as unknown.  A provider's literal zero for a
    # movement leg would falsely create exact downstream timing.
    for day in result.days:
        for leg in day.transport_legs:
            if leg.uncertainty and leg.duration_minutes == 0:
                failures.append("Unknown route duration was represented as zero.")
    unresolved_ids = {
        poi_id
        for poi_id, value in mandatory_allocation.items()
        if value.get("status") == "ALLOCATED_UNRESOLVED"
    }
    # Scheduler records unknown critical timing as a hard issue.  It remains a
    # BEST_EFFORT uncertainty when it is preserved as unknown, not a FAIL.
    hard = []
    for item in issues:
        if item.severity != "hard" or item.code in {
            "CRITICAL_ROUTE_UNKNOWN", "PLANNER_DEADLINE_REACHED",
        }:
            continue
        if item.code == "MISSING_MANDATORY_POI" and any(
            item.message == f"Mandatory POI not scheduled: {poi.name}"
            for poi in world.input.pois if poi.id in unresolved_ids
        ):
            continue
        hard.append(item)
    if hard:
        failures.append("Hard deterministic validation issue exists.")
    terminal_gaps = terminal_connection_gaps(result, world)
    critical_timing_unknown = any(
        getattr(day, "schedule_state", "complete") == "unresolved_transfer"
        or any(getattr(leg, "critical", True) and (
            leg.duration_minutes is None or getattr(leg, "uncertainty", False)
        ) for leg in day.transport_legs)
        for day in result.days
    )
    unresolved = bool(
        terminal_gaps
        or critical_timing_unknown
        or result.unsatisfied_mandatory_requirements
        or any(v.get("status") != "SCHEDULED" for v in mandatory_allocation.values())
        or any(day.provisional_activities for day in result.days)
        or result.planning_metrics.get("unknown_route_legs", 0)
        or any(
            v.get("status") == "ALLOCATED_UNRESOLVED"
            for v in mandatory_allocation.values()
        )
    )
    status = "FAIL" if failures else "BEST_EFFORT" if unresolved else "PASS"
    mandatory_honored = not result.unsatisfied_mandatory_requirements and not any(
        value.get("status") != "SCHEDULED" for value in mandatory_allocation.values()
    )
    # A structurally valid shell with no scheduled activity is useful only as a
    # draft.  It must not be presented as booking-ready merely because this
    # particular trip has no explicit must-see requirement.
    has_scheduled_activity = bool(scheduled)
    # A BEST_EFFORT plan is publishable only when all mandatory content is
    # present and the remaining uncertainty is visible rather than fabricated.
    publishable = (not failures and mandatory_honored and has_scheduled_activity
                   and not terminal_gaps
                   and not critical_timing_unknown
                   and not any(day.provisional_activities for day in result.days))
    has_grounded_days = bool(getattr(result, "days", []))
    delivery_status = (
        "BLOCKED" if status == "FAIL" else
        "READY" if status == "PASS" and publishable else
        "READY_WITH_WARNINGS" if status == "BEST_EFFORT" and publishable else
        "DRAFT_NEEDS_CONFIRMATION" if has_grounded_days else "BLOCKED"
    )
    return {
        "status": status,
        "grounding_ok": not any("outside Research" in item for item in failures),
        "mandatory_consistent": not any(
            "Mandatory" in item or "mandatory" in item for item in failures
        ),
        "route_consistent": not any(
            "route" in item.lower() or "Route" in item for item in failures
        ),
        "scheduler_consistent": not bool(hard),
        "structural_complete": route_plan.structural_complete,
        "missing_legs": route_plan.metrics.get("missing_legs", 0),
        "mandatory_unresolved": [
            poi
            for poi, v in mandatory_allocation.items()
            if v.get("status") == "ALLOCATED_UNRESOLVED"
        ],
        "failures": failures,
        "hard_issue_count": len(hard),
        "publishable": publishable,
        "delivery_status": delivery_status,
        "blocking_reasons": failures + terminal_gaps
        + (["Critical travel timing needs confirmation."] if critical_timing_unknown else [])
        + ([] if mandatory_honored else ["Mandatory requirements are not fully scheduled."])
        + ([] if has_scheduled_activity else ["No researched activity could be scheduled with the available route and timing evidence."]),
    }
