"""Route-optional recommendations view; never changes a saved plan or its verdict."""

from copy import deepcopy
from typing import Any

from .contracts import CostRecord, TripPlanV2


def _cost_view(cost: CostRecord) -> dict[str, Any]:
    result = cost.model_dump(mode="json")
    # A zero subtotal without priced components means unknown, not free travel.
    has_costs = bool(cost.category_totals) or cost.known_subtotal > 0
    if not has_costs:
        result["known_subtotal"] = None
    if not has_costs or cost.unknown_components:
        result["budget_status"] = "unknown"
    return result


def discovery_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """Project a validated trip_plan.v2 into a smaller discovery display contract.

    Costs come from the existing validated trip ledger, not an additional sum
    of daily totals. Currency subtotals are not converted here. This is a view,
    not a new agent run or a route-free replacement for feasibility validation.
    """
    plan = TripPlanV2.model_validate(payload)
    days = []
    for day in plan.days:
        pois = [
            {
                "id": event.id,
                "name": event.title,
                "place_id": event.place_id,
                "reason": event.reason,
                "visit_minutes": event.visit_minutes,
                "duration_basis": event.duration_basis,
                "period": event.period,
                "estimated_cost": event.estimated_cost,
                "currency": event.currency,
                "evidence": [item.model_dump(mode="json") for item in event.evidence],
            }
            for event in day.events if event.kind == "activity"
        ]
        other_events = [
            {"kind": event.kind, "title": event.title, "reason": event.reason, "period": event.period}
            for event in day.events if event.kind != "activity"
        ]
        meaningful = pois or any(event.kind in {"travel", "rest"} for event in day.events)
        days.append({
            "day": day.day, "date": day.date.isoformat(),
            "base_location": day.base_location, "pois": pois,
            "title": day.title, "summary": day.summary,
            "workload": day.workload,
            "other_events": other_events,
            "timeline": [event.model_dump(mode="json", include={
                "id", "kind", "title", "reason", "period", "visit_minutes", "duration_basis",
            }) for event in day.events],
            "content_status": "suggestions" if meaningful else "pending",
            "cost": _cost_view(day.daily_cost),
        })
    return {
        "schema_version": "trip_discovery.v1",
        "planning_profile": plan.planning_profile,
        "route_profiles": plan.route_profiles,
        "scope": "recommendations_not_verified_schedule",
        "source_status": plan.status,
        "trip_summary": plan.trip_summary.model_dump(mode="json"),
        "days": days,
        "accommodation": [item.model_dump(mode="json") for item in plan.accommodation],
        "transport": [item.model_dump(mode="json") for item in plan.transport],
        "weather": [dict(item.model_dump(mode="json"), location=next(
            (d.label for d in plan.destinations if d.id == item.destination_id),
            item.destination_id)) for item in plan.weather],
        "cost": _cost_view(plan.total_estimated_cost),
        "budget_guidance": plan.budget_guidance.model_dump(mode="json") if plan.budget_guidance else None,
        "local_tips": plan.local_tips,
        "packing_list": plan.packing_list,
        "warnings": deepcopy(payload.get("warnings", [])),
    }
