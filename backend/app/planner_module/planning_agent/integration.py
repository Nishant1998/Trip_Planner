"""Adapt the checked planner artifact to the existing pipeline result state."""
from ..adapters import build_world
from ..models.planning import DayRouteChain, RouteLeg, TripRoutePlan, ValidationIssue
from .rendering import render_checked_artifact
from .phase_checks import reached_destinations


def assemble_pipeline_state(artifact, data, step_facts):
    result = render_checked_artifact(artifact, data, step_facts)
    world = build_world(data)
    checked = artifact["validation"]
    issues = [ValidationIssue(code=item["code"],
        severity="hard" if item.get("outcome") == "fail" else "warning",
        day_number=item.get("day_number"), message=item.get("message", item["code"]),
        entity_ids=item.get("entity_ids", [])) for item in checked["issues"]]
    chains = []
    for day in result.days:
        travel_blocks = [block for block in day.timeline if block.type.value == "travel"]
        legs = [RouteLeg(id=block.id, origin_node_id=leg.origin_id, destination_node_id=leg.destination_id,
            origin_name=leg.origin_name or leg.origin_id, destination_name=leg.destination_name or leg.destination_id,
            mode=leg.mode, distance_km=leg.distance_km, duration_minutes=leg.duration_minutes,
            status=leg.status, source=leg.provenance, evidence_ids=leg.evidence_ids,
            uncertainty=leg.uncertainty) for block, leg in zip(travel_blocks, day.transport_legs)]
        day_check = checked.get("timelines", {}).get(str(day.day_number), {})
        contiguous = bool(day.timeline) and not any(item["code"] in {
            "LOCATION_DISCONTINUITY", "WRONG_ENDPOINT"} for item in day_check.get("issues", []))
        chains.append(DayRouteChain(day_number=day.day_number,
            start_anchor_id=day.start_anchor_id or "unknown", end_anchor_id=day.end_anchor_id or "unknown",
            day_kind=day.day_kind, ordered_node_ids=[day.start_anchor_id or "unknown", *[leg.destination_node_id for leg in legs]],
            route_legs=legs, structural_complete=contiguous,
            resolved_leg_count=sum(leg.duration_minutes is not None for leg in legs),
            unknown_leg_count=sum(leg.duration_minutes is None for leg in legs)))
    structure_checked = checked.get("structural_complete") is True
    route_plan = TripRoutePlan(days=chains, structural_complete=structure_checked,
        timing_complete=bool(chains) and all(day.schedule_state == "complete" for day in result.days),
        warnings=[] if structure_checked else ["Traveler-state or terminal-link completeness remains unresolved."])
    allocations = {}
    for poi_id in world.mandatory_poi_ids:
        allocation = {"status": "UNSATISFIED", "day_number": None, "reason": "Required visit is not scheduled."}
        for day in result.days:
            if any(activity.poi_id == poi_id for activity in day.activities):
                allocation = {"status": "SATISFIED", "day_number": day.day_number, "reason": None}
            elif any(activity.poi_id == poi_id for activity in day.provisional_activities):
                allocation = {"status": "ALLOCATED_UNRESOLVED", "day_number": day.day_number,
                    "reason": "Visit is proposed with explicit timing or availability qualifications."}
        allocations[poi_id] = allocation
    reached = reached_destinations({key: value for key, value in checked.get("timelines", {}).items()
                                    if value["outcome"] == "pass"}, data)
    for requirement in data.requirements:
        if not requirement.required:
            continue
        mapped = {poi.id for poi in data.pois if requirement.id in poi.requirement_ids}
        if requirement.requirement_type == "destination_or_base":
            targets = {stop.id for stop in data.stops if requirement.id in stop.requirement_ids}
            satisfied = bool(targets & reached)
        else:
            satisfied = bool(mapped) and all(allocations.get(key, {}).get("status") == "SATISFIED" for key in mapped)
        (result.satisfied_mandatory_requirements if satisfied else result.unsatisfied_mandatory_requirements).append(requirement.id)
    validation = {**checked, "status": "PASS" if artifact["status"] == "READY" else "BEST_EFFORT",
        "delivery_status": artifact["status"], "publishable": artifact["status"] == "READY",
        "structural_complete": route_plan.structural_complete,
        "missing_legs": sum(chain.unknown_leg_count for chain in chains),
        "issues": [issue.model_dump(mode="json") for issue in issues]}
    return {"result": result, "world": world, "route_plan": route_plan,
        "mandatory_allocation": allocations, "final_issues": issues,
        "final_validation": validation, "metrics": result.planning_metrics}
