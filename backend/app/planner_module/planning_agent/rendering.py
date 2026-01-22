"""Render checked artifacts without rescheduling or retrieving new facts."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta

from ..models.planning import Activity, BudgetSummary, DayPlan, PlanningResult, ScheduleBlock, TransportLeg
from .contracts import TripDraft, fingerprint
from .route_tool import stay_anchor_id
from .presentation_guard import presentation_options, validate_presentation


def render_checked_artifact(artifact, data, step_facts):
    draft = TripDraft.model_validate(artifact["draft"])
    validation = artifact["validation"]
    if (artifact["input_fingerprint"] != fingerprint(data)
        or validation["draft_hash"] != fingerprint(artifact["draft"])
        or validation["plan_version"] != artifact["plan_version"]
        or validation["evidence_revision"] != artifact["evidence_revision"]):
        raise ValueError("Artifact does not match its validated version")
    if artifact["status"] == "READY" and validation.get("ready") is not True:
        raise ValueError("Unaccepted artifact cannot be rendered as complete")
    names = {item.id: item.name for item in [*data.stops, *data.pois]}
    names.update(origin_anchor=data.origin, end_anchor=data.end_location or data.origin)
    names.update({stay_anchor_id(stay): stay["name"] for stay in data.stay_bases if stay.get("name")})
    pois = {poi.id: poi for poi in data.pois}
    days = []
    for day in draft.days:
        checked = validation.get("timelines", {}).get(str(day.day_number))
        blocks, activities, provisional, legs = [], [], [], []
        warnings = list(day.notes)
        warnings.extend(item["message"] for item in validation.get("disclosures", [])
                        if item.get("day_number") == day.day_number)
        if checked is not None:
            if day.timeline is None or [row["id"] for row in checked["timeline"]] != [s.id for s in day.timeline.steps]:
                raise ValueError("Checked timeline differs from committed steps")
            for step, row in zip(day.timeline.steps, checked["timeline"]):
                if (row["origin_id"], row["destination_id"]) != (step.origin_id, step.destination_id):
                    raise ValueError("Checked traveler state differs from committed steps")
                start, end = row["start"] or "unknown", row["end"] or "unknown"
                issues = [i["code"] for i in checked["issues"] if i.get("step_id") == step.id]
                unresolved = bool(issues) or not row["start"] or not row["end"] or checked["outcome"] == "fail"
                uncertain = unresolved or row.get("timing_estimated", False) or step.basis in {"unknown", "planning_allowance", "estimate"}
                status = "requires_confirmation" if unresolved else "estimated" if uncertain else "complete"
                notes = [f"Timing basis: {step.basis}", *issues]
                wait_minutes = row.get("wait_minutes")
                if wait_minutes and row["start"]:
                    wait_end = datetime.fromisoformat(row["start"])
                    wait_start = (wait_end.astimezone(UTC) - timedelta(minutes=wait_minutes)).astimezone(wait_end.tzinfo)
                    blocks.append(ScheduleBlock(id=f"wait-before:{step.id}", type="wait",
                        start_time=wait_start.isoformat(), end_time=row["start"], duration_minutes=wait_minutes,
                        origin_id=step.origin_id, destination_id=step.origin_id,
                        grounded=not uncertain, status=status,
                        notes=["Waiting time from the checked timeline."]))
                kind = {"visit": "activity", "check_in": "buffer", "check_out": "buffer"}.get(step.kind, step.kind)
                blocks.append(ScheduleBlock(id=step.id, type=kind, start_time=start, end_time=end,
                    duration_minutes=step.minutes, origin_id=step.origin_id, destination_id=step.destination_id,
                    poi_id=step.origin_id if step.kind == "visit" else None,
                    grounded=step.basis == "evidence", status=status,
                    notes=notes, scheduled_service=step.scheduled_service))
                if step.kind == "visit":
                    poi = pois[step.origin_id]
                    if step.minutes is None:
                        warnings.append(f"{poi.name}: duration unknown; retained in timeline without invented visit length.")
                        continue
                    activity = Activity(poi_id=poi.id, name=poi.name, start_time=start, end_time=end,
                        planned_visit_minutes=step.minutes, mandatory=poi.is_mandatory,
                        requirement_ids=poi.requirement_ids, address=poi.address,
                        opening_hours_status="unknown" if issues else "within_researched_window",
                        notes=notes, evidence_ids=poi.evidence_ids)
                    (provisional if unresolved or poi.id in day.confirmation_required_poi_ids else activities).append(activity)
                elif step.kind == "travel":
                    fact = step_facts.get(step.id, {})
                    evidence = fact.get("route_evidence", {})
                    legs.append(TransportLeg(origin_id=step.origin_id, destination_id=step.destination_id,
                        mode=fact.get("route_mode", "unknown"), start_time=start, end_time=end,
                        duration_minutes=step.minutes, status=evidence.get("status", "unknown" if step.minutes is None else "estimated"),
                        distance_km=evidence.get("distance_km"), evidence_ids=evidence.get("evidence_ids", []),
                        provenance=evidence.get("provenance", "unknown"), mode_resolution=evidence.get("mode_resolution"),
                        scheduled_service=step.scheduled_service, uncertainty=uncertain or not bool(evidence),
                        origin_name=names.get(step.origin_id), destination_name=names.get(step.destination_id)))
        else:
            warnings.append("This day has no validated timeline; do not treat it as scheduled.")
            # The delivery gate may reject the detailed clock without rejecting
            # the model's day-level allocation.  Keep those researched places
            # visible as unresolved proposals instead of rendering a blank day.
            # They remain outside ``activities`` and therefore cannot be
            # mistaken for validated/scheduled visits.
            for poi_id in day.selected_poi_ids:
                poi = pois.get(poi_id)
                if poi is None:
                    continue
                allowance = day.visit_allowances.get(poi_id)
                minutes = allowance.minutes if allowance else (
                    poi.recommended_visit_minutes or poi.visit_minutes
                )
                provisional.append(Activity(
                    poi_id=poi.id, name=poi.name, start_time="unknown", end_time="unknown",
                    planned_visit_minutes=minutes, mandatory=poi.is_mandatory,
                    requirement_ids=poi.requirement_ids, address=poi.address,
                    opening_hours_status="requires_confirmation",
                    notes=["The visit is allocated to this day, but its timeline did not pass validation."],
                    evidence_ids=poi.evidence_ids,
                ))
        rows = checked["timeline"] if checked else []
        days.append(DayPlan(day_number=day.day_number, date=day.date.isoformat(), title=day.theme,
            overnight_base=names.get(day.overnight_anchor_id), day_start=blocks[0].start_time if blocks else "unknown",
            day_end=rows[-1]["end"] or "unknown" if rows else "unknown",
            start_anchor=names.get(day.start_anchor_id), end_anchor=names.get(day.end_anchor_id),
            start_anchor_id=day.start_anchor_id, end_anchor_id=day.end_anchor_id, day_kind=day.day_kind,
            route_node_ids=([rows[0]["origin_id"], *[row["destination_id"] for row in rows
                if row["origin_id"] != row["destination_id"]]] if rows else []),
            schedule_state="complete" if checked and checked["outcome"] == "pass" else "partial_timing",
            timeline=blocks, activities=activities, provisional_activities=provisional, transport_legs=legs,
            warnings=warnings, confirm_before_visit=[i["code"] for i in checked["issues"]] if checked else ["TIMELINE_MISSING"]))
    budget = validation.get("budget", {})
    selected = {identity for day in draft.days for identity in day.selected_poi_ids}
    presentation = None
    if artifact.get("presentation"):
        try:
            presentation = validate_presentation(artifact["presentation"]["text"], artifact, data)
        except ValueError:
            presentation = presentation_options(artifact, data)
    return PlanningResult(status="complete" if artifact["status"] == "READY" else "best_effort" if days else "invalid_input",
        trip_summary=presentation["introduction"] if presentation else data.trip_name,
        selected_bases=draft.base_sequence, nights_by_base=draft.nights_by_base,
        days=days, unscheduled_optional_pois=[p.id for p in data.pois if not p.is_mandatory and p.id not in selected],
        uncertainties=draft.unresolved_items,
        warnings=[i.get("message", i["code"]) for i in [*validation["issues"], *validation.get("disclosures", [])]],
        budget_summary=BudgetSummary(currency=data.currency, status=budget.get("status", "unknown"),
            categories=deepcopy(budget.get("categories", {})),
            total_verified=budget.get("total_verified", 0), total_estimated=budget.get("total_estimated", 0),
            known_attraction_cost=budget.get("categories", {}).get("activities", {}).get("verified_subtotal", 0),
            unknown_categories=budget.get("unknown_categories", []),
            notes=["Amounts are supported subtotals, not a complete trip price when categories are unknown.",
                   "Estimated subtotals include lower bounds of supported ranges; planning allowances are excluded."]),
        planning_metrics={"delivery_status": artifact["status"], "plan_version": artifact["plan_version"],
            "evidence_revision": artifact["evidence_revision"], "draft_hash": validation["draft_hash"],
            "presentation": presentation,
            "checked_budget": deepcopy(budget), "cost_items": [item.model_dump(mode="json") for item in draft.costs]})
