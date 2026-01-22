"""No-network whole-draft checks and evidence-current candidate selection."""
from collections import Counter
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfoNotFoundError

from .contracts import VersionedDraft, fingerprint
from .phase_checks import check_draft, reached_destinations
from .timeline import local_instant
from .constraints import build_constraint_ledger
from .cost_coverage import apply_coverage_issues, quantity_issues
from .expense_scope import expense_scope_issues
from .claims import relevant_conflicts
from .hard_limits import check_daily_limits
from .accessibility import check_accessibility
from .hard_review import check_hard_review
from ..tools.transport import normalize_mode


class DeliveryGate:
    def __init__(self, utilities, *, explicit_order=()):
        self.utilities = utilities
        self.explicit_order = tuple(explicit_order)

    async def validate(self, candidate, data):
        structural = check_draft(candidate.draft, data, phase="city_details", explicit_order=self.explicit_order,
                                 registered_anchors=self.utilities.entity_ids,
                                 anchor_bases={item["id"]: item["base_id"] for item in self.utilities.anchor_catalog})
        issues = [issue.model_dump(mode="json") for issue in structural.issues]
        disclosures = []
        for claim in relevant_conflicts(self.utilities.claims, candidate.draft):
            issues.append({"code": "EVIDENCE_CONFLICT", "outcome": "unknown",
                "message": "A retrieved claim conflicts with evidence used by this itinerary.",
                "entity_ids": [claim["entity_id"]]})
        timelines = {}
        previous_end = None
        previous_timing_unknown = False
        terminal_gaps = False
        anchors = {item["id"]: item for item in self.utilities.anchor_catalog}
        def issue(code, message, day=None, outcome="fail"):
            issues.append({"code": code, "message": message, "day_number": day, "outcome": outcome})
        for day in candidate.draft.days:
            if day.overnight_kind == "stay":
                anchor = anchors.get(day.overnight_anchor_id, {})
                if anchor.get("role") not in {"base", "stay_area", "origin", "end"}:
                    issue("STAY_LOCATION_UNSUPPORTED", "The overnight location is not a researched stay area or a known planning base.", day.day_number, "unknown")
                else:
                    disclosures.append({"code": "APPROXIMATE_STAY_BASE", "day_number": day.day_number,
                        "message": "Overnight location is a planning base/area, not a confirmed hotel or booking."})
            timeline = day.timeline
            if timeline is None:
                issue("TIMELINE_MISSING", "Day has no checked timeline.", day.day_number, "unknown")
                previous_end = None
                previous_timing_unknown = True
                continue
            if timeline.start_time.date() != day.date:
                issue("TIMELINE_DATE", "Timeline does not begin on its assigned day.", day.day_number)
            if (timeline.start_anchor_id, timeline.end_anchor_id) != (day.start_anchor_id, day.end_anchor_id):
                issue("TIMELINE_ANCHORS", "Timeline anchors differ from the committed day.", day.day_number)
            selected = Counter(day.selected_poi_ids)
            visited = Counter(step.origin_id for step in timeline.steps if step.kind == "visit")
            if selected != visited:
                issue("TIMELINE_VISIT_SET", "Timeline must retain exactly the assigned visits.", day.day_number)
            if not timeline.steps:
                issue("EMPTY_DAY", "No traveler activity or movement is represented.", day.day_number)
            if day.day_kind.value == "rest_or_buffer" and not day.notes:
                issue("REST_REASON", "Intentional rest day needs its planning reason.", day.day_number)
            for step_index, step in enumerate(timeline.steps):
                if step.kind != "travel":
                    continue
                fact = self.utilities.step_facts.get(step.id, {})
                mode = normalize_mode(fact.get("route_mode")).value
                left, right = anchors.get(step.origin_id, {}), anchors.get(step.destination_id, {})
                resolution = fact.get("route_evidence", {}).get("mode_resolution") or {}
                movement = resolution.get("movement_class")
                if movement is None and left.get("base_id") and right.get("base_id"):
                    movement = "local" if left["base_id"] == right["base_id"] else "inter_base"
                if mode == "unknown" or (mode in {"train", "bus", "transit"} and movement in {None, "unknown"}):
                    terminal_gaps = True
                    issue("MOVEMENT_SCOPE_UNKNOWN", "Transport mode or local/intercity scope is not established.", day.day_number, "unknown")
                needs_terminals = mode == "flight" or (mode in {"train", "bus", "transit"} and movement in {"inter_base", "international"})
                if needs_terminals and (left.get("role") != "terminal" or right.get("role") != "terminal"):
                    terminal_gaps = True
                    issue("TERMINAL_LINKS_UNRESOLVED", "Major public transport needs resolved terminals and their access/egress legs.", day.day_number, "unknown")
                if needs_terminals:
                    preparation = False
                    for prior in reversed(timeline.steps[:step_index]):
                        if prior.origin_id != step.origin_id or prior.destination_id != step.origin_id:
                            break
                        if prior.kind == "buffer" and prior.minutes is not None and prior.minutes > 0:
                            preparation = True
                    if not preparation:
                        issue("TERMINAL_BUFFER_MISSING", "Major transport needs an explicit preparation buffer at its departure terminal.", day.day_number, "unknown")
            try:
                start_instant = local_instant(timeline.start_time, timeline.timezone)
                if previous_end is not None and start_instant < previous_end:
                    issue("CROSS_DAY_OVERLAP", "Day begins before the preceding journey finishes.", day.day_number)
                if previous_timing_unknown:
                    issue("PRIOR_ARRIVAL_UNKNOWN", "Previous day's arrival time is unresolved.", day.day_number, "unknown")
                result = await self.utilities.check_timeline(timeline.model_dump(mode="json"))
            except (ValueError, ZoneInfoNotFoundError) as exc:
                issue("TIMELINE_EVIDENCE",
                    f"Timeline contains invalid or unregistered facts: {exc}",
                    day.day_number)
                previous_end = None
                previous_timing_unknown = True
                continue
            timelines[str(day.day_number)] = result
            ends = result["timeline"]
            end_text = ends[-1]["end"] if ends else None
            previous_end = datetime.fromisoformat(end_text).astimezone(UTC) if end_text else None
            previous_timing_unknown = previous_end is None
            hard_end = data.time_preferences.get("hard_day_end")
            if hard_end is not None:
                try:
                    wall_time = time.fromisoformat(hard_end)
                    if wall_time.tzinfo is not None:
                        raise ValueError("Daily boundary must be a local wall time")
                    cutoff = local_instant(datetime.combine(day.date, wall_time), timeline.timezone)
                except (ValueError, TypeError, ZoneInfoNotFoundError):
                    issue("HARD_DAY_END_UNRESOLVED", "Confirmed day-end time cannot be interpreted unambiguously.", day.day_number, "unknown")
                else:
                    if previous_end is None:
                        issue("HARD_DAY_END_UNRESOLVED", "Unknown arrival time cannot satisfy the confirmed day-end limit.", day.day_number, "unknown")
                    elif previous_end > cutoff:
                        issue("HARD_DAY_END_EXCEEDED", "The day finishes after the confirmed hard day-end limit.", day.day_number)
            if end_text:
                end_date = datetime.fromisoformat(end_text).date()
                if end_date > day.date:
                    if day.overnight_kind != "in_transit" or not timeline.steps or timeline.steps[-1].kind != "travel":
                        issue("UNDECLARED_OVERNIGHT", "Cross-midnight activity needs an explicit overnight travel state.", day.day_number)
                    if end_date > day.date + timedelta(days=1):
                        issue("MULTIDAY_BLOCK", "Travel cannot silently consume additional calendar days.", day.day_number)
                if end_date.isoformat() > data.end_date:
                    issue("AFTER_TRIP_END", "The journey finishes after the confirmed trip dates.", day.day_number)
            for item in result["issues"]:
                issues.append({**item, "day_number": day.day_number})
        reached = reached_destinations(timelines, data)
        for requirement in data.requirements:
            if requirement.required and requirement.requirement_type == "destination_or_base":
                targets = {stop.id for stop in data.stops if requirement.id in stop.requirement_ids}
                if not targets.intersection(reached):
                    issue("DESTINATION_NOT_REACHED", "A required destination has no checked traveler-state presence.")
        try:
            budget = await self.utilities.calculate({"operation": "budget",
                "items": [item.model_dump(mode="json") for item in candidate.draft.costs],
                "currency": data.currency, "cap": data.budget.get("total", data.budget.get("amount"))})
        except ValueError:
            budget = {"outcome": "fail", "reason": "unregistered_or_invalid_costs"}
        selected = {pid for day in candidate.draft.days for pid in day.selected_poi_ids}
        costs = {item.id: item for item in candidate.draft.costs}
        admission_gaps = []
        adults = data.travellers.get("num_adults")
        children = data.travellers.get("num_children", 0)
        for poi in data.pois:
            if poi.id not in selected:
                continue
            for prefix, count in (("adult", adults), ("child", children)):
                if isinstance(count, int) and not isinstance(count, bool) and count == 0:
                    continue
                if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                    admission_gaps.append("ADMISSION_PARTY_UNKNOWN")
                    issue("ADMISSION_PARTY_UNKNOWN", f"The {prefix} count for admission at {poi.id} is unresolved.", outcome="unknown")
                    continue
                item = costs.get(f"{prefix}_entry:{poi.id}")
                if item is None:
                    admission_gaps.append("ADMISSION_COST_MISSING")
                    issue("ADMISSION_COST_MISSING", f"Selected visit {poi.id} has no explicit {prefix} admission cost coverage in the ledger.", outcome="unknown")
                elif item.quantity != count or item.unit != "person" or item.category != "activities":
                    admission_gaps.append("ADMISSION_QUANTITY")
                    issue("ADMISSION_QUANTITY", f"The {prefix} admission item for {poi.id} must cover the confirmed count in person units under activities.")
                claim_dates = {entry["applicable_date"] for entry in self.utilities.claims.values()
                    if entry.get("field") == "admission" and entry.get("entity_id") == poi.id
                    and entry.get("admission", {}).get("audience") == prefix and entry.get("status") == "accepted"}
                selected_dates = {day.date.isoformat() for day in candidate.draft.days if poi.id in day.selected_poi_ids}
                if claim_dates and not selected_dates <= claim_dates:
                    admission_gaps.append("ADMISSION_DATE_UNRESOLVED")
                    issue("ADMISSION_DATE_UNRESOLVED", f"Admission evidence for {poi.id} does not cover the selected date.", outcome="unknown")
        if admission_gaps:
            budget["coverage_issues"] = sorted(set(admission_gaps))
            budget["unknown_categories"] = sorted(set(budget.get("unknown_categories", [])) | {"activities"})
            budget["upper_estimate"] = None
            if "categories" in budget:
                budget["categories"]["activities"]["status"] = "unknown"
                budget["categories"]["activities"]["upper_estimate"] = None
            if budget.get("status") == "known":
                budget["status"] = "partial"
            if "ADMISSION_QUANTITY" in admission_gaps:
                budget["outcome"] = "fail"
            elif budget["outcome"] == "pass":
                budget["outcome"] = "unknown"
        quantity_gaps = quantity_issues(candidate.draft, data) + expense_scope_issues(candidate.draft, self.utilities.claims)
        issues.extend(quantity_gaps)
        apply_coverage_issues(budget, quantity_gaps)
        if budget["outcome"] != "pass":
            issue("BUDGET_UNRESOLVED", "Full-trip affordability is not established.", outcome=budget["outcome"])
        # Free-text hard constraints cannot become verified just because the
        # schedule passed. Their structured policy checks must supply evidence.
        hard_issues, hard_checks = check_hard_review(candidate, data, self.utilities)
        issues.extend(hard_issues)
        if any(row["outcome"] == "pass" for row in hard_checks):
            disclosures.append({"code": "SEMANTIC_CONSTRAINT_REVIEW",
                "message": "Free-text requirements were assessed by the reviewer; this judgment does not certify missing facts or override numerical checks."})
        access_issues, access_checks = check_accessibility(candidate.draft, data, self.utilities.claims)
        issues.extend(access_issues)
        if any(row["outcome"] == "pass" for row in access_checks):
            disclosures.append({"code": "INTERPRETED_ACCESSIBILITY_EVIDENCE",
                "message": "Accessibility support is interpreted from cited sources, not an independently verified guarantee or reservation."})
        limit_issues, limit_checks = check_daily_limits(candidate.draft, data, self.utilities.step_facts, timelines)
        issues.extend(limit_issues)
        ready = bool(candidate.draft.days) and not issues
        ledger = build_constraint_ledger(data, explicit_order=self.explicit_order)
        structural_complete = (bool(candidate.draft.days) and not structural.issues and not terminal_gaps
            and len(timelines) == len(candidate.draft.days)
            and not any(item["code"] in {"LOCATION_DISCONTINUITY", "WRONG_ENDPOINT", "TIMELINE_ANCHORS",
                "EMPTY_DAY", "DESTINATION_NOT_REACHED"} for item in issues))
        return {"ready": ready, "issues": issues, "timelines": timelines, "budget": budget,
                "disclosures": disclosures, "hard_limit_checks": limit_checks, "accessibility_checks": access_checks,
                "hard_constraint_checks": hard_checks,
                "structural_complete": structural_complete,
                "constraint_ledger": ledger.model_dump(mode="json"), "constraint_hash": fingerprint(ledger),
                "policy_version": ledger.policy_version,
                "plan_version": candidate.plan_version, "evidence_revision": candidate.evidence_revision,
                "draft_hash": fingerprint(candidate.draft)}

    async def finalize(self, candidate, data, history, reason):
        candidates = [VersionedDraft.model_validate(raw) for raw in history]
        candidates.append(candidate)
        checked = []
        mandatory_pois = {poi.id for poi in data.pois if poi.is_mandatory}
        for value in candidates[-5:]:
            value = value.model_copy(update={"evidence_revision": candidate.evidence_revision}, deep=True)
            report = await self.validate(value, data)
            failures = sum(item["outcome"] == "fail" for item in report["issues"])
            unknowns = sum(item["outcome"] == "unknown" for item in report["issues"])
            useful_days = len(report["timelines"])
            selected_pois = {poi_id for day in value.draft.days for poi_id in day.selected_poi_ids}
            # A qualified candidate with the same number of hard failures must
            # not win merely by dropping must-sees, optional day content, or
            # the budget ledger.  Prefer preserving completed useful work,
            # then use unknown count and checked timelines as tie-breakers.
            checked.append(((failures, -len(selected_pois & mandatory_pois),
                -len(selected_pois), -len(value.draft.costs), unknowns,
                -useful_days, -value.plan_version), value, report))
        _, selected, report = min(checked, key=lambda item: item[0])
        status = ("READY" if report["ready"] else "DRAFT_NEEDS_CONFIRMATION"
                  if selected.draft.days else "BLOCKED")
        if reason in {"cancelled", "bridge_session_expired"}:
            status = "DRAFT_NEEDS_CONFIRMATION" if selected.draft.days else "BLOCKED"
        return {"status": status, "stop_reason": reason, "plan_version": selected.plan_version,
                "evidence_revision": candidate.evidence_revision, "validation": report,
                "draft": selected.draft.model_dump(mode="json"),
                "input_fingerprint": candidate.input_fingerprint}
