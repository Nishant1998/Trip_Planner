"""Source-bound accessibility observations for the exact selected traveler states."""
from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AccessibilityValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirement_index: int = Field(ge=0)
    requirement_text: str = Field(min_length=1, max_length=4000)
    target_kind: Literal["visit", "travel", "stay"]
    outcome: Literal["supported", "unsupported", "unknown"]
    detail: str = Field(min_length=1, max_length=1000)


def accessibility_targets(draft):
    targets = {}
    for day in draft.days:
        date = day.date.isoformat()
        if day.timeline:
            for step in day.timeline.steps:
                if step.kind in {"visit", "travel"}:
                    targets[(date, step.kind, step.id)] = {
                        "entity_ids": [step.origin_id, step.destination_id],
                        "state": step.model_dump(mode="json"), "day_number": day.day_number}
        # A selected visit without a timeline must still be accounted for.
        scheduled = {step.origin_id for step in day.timeline.steps if step.kind == "visit"} if day.timeline else set()
        for entity in set(day.selected_poi_ids) - scheduled:
            targets[(date, "visit", f"unscheduled:{entity}")] = {
                "entity_ids": [entity], "state": {"unscheduled": True}, "day_number": day.day_number}
        if day.overnight_kind == "stay":
            targets[(date, "stay", day.overnight_anchor_id)] = {
                "entity_ids": [day.overnight_anchor_id], "state": {"anchor": day.overnight_anchor_id},
                "day_number": day.day_number}
    return targets


def bind_accessibility(claim, draft, data):
    if draft is None or data is None:
        raise ValueError("Accessibility evidence requires confirmed input and the dated draft")
    value = claim.accessibility
    needs = data.travellers.get("accessibility_needs", [])
    if (not isinstance(needs, list) or value.requirement_index >= len(needs)
            or needs[value.requirement_index] != value.requirement_text):
        raise ValueError("Accessibility observation must quote the confirmed requirement")
    target = accessibility_targets(draft).get((str(claim.applicable_date), value.target_kind, claim.fact_id))
    if target is None or claim.entity_id not in target["entity_ids"]:
        raise ValueError("Accessibility observation does not identify a selected dated target")
    return sha256(json.dumps(target, sort_keys=True).encode()).hexdigest()


def check_accessibility(draft, data, claims):
    needs = data.travellers.get("accessibility_needs", [])
    if not needs:
        return [], []
    issues, checks = [], []
    if not isinstance(needs, list) or any(not isinstance(need, str) or not need for need in needs):
        return [{"code": "ACCESSIBILITY_UNVERIFIED", "outcome": "unknown",
                 "message": "Accessibility requirements are not a list of nonempty confirmed needs."}], []
    targets = accessibility_targets(draft)
    for index, need in enumerate(needs):
        for (date, kind, target_id), target in targets.items():
            matching = []
            for claim in claims.values():
                value = claim.get("accessibility") or {}
                if (claim.get("field") == "accessibility" and claim.get("applicable_date") == date
                        and claim.get("fact_id") == target_id and value.get("target_kind") == kind
                        and value.get("requirement_index") == index and value.get("requirement_text") == need):
                    digest = sha256(json.dumps(target, sort_keys=True).encode()).hexdigest()
                    if claim.get("accessibility_scope_hash") == digest:
                        matching.append(claim)
            outcomes = {(row.get("accessibility") or {}).get("outcome") for row in matching}
            # Negative evidence cannot be overridden by a second positive observation.
            outcome = "fail" if "unsupported" in outcomes else "pass" if outcomes == {"supported"} else "unknown"
            if kind == "stay" and outcome == "pass":
                # Current anchors identify areas, not a booked accessible room/property.
                outcome = "unknown"
            record = {"constraint_id": f"accessibility:{index}", "day_number": target["day_number"],
                "date": date, "kind": kind, "target_id": target_id, "entity_ids": target["entity_ids"],
                "outcome": outcome, "source_ids": sorted({row["source_id"] for row in matching}),
                "basis": "source_bound_model_interpretation" if matching else "missing_evidence",
                "independently_verified": False}
            checks.append(record)
            if outcome != "pass":
                issues.append({**record, "code": "ACCESSIBILITY_UNSUPPORTED" if outcome == "fail" else "ACCESSIBILITY_UNVERIFIED",
                    "message": f"{need}: {kind} {target_id} needs an accessible alternative or applicable evidence; a base area does not establish accessible accommodation."})
        if not targets:
            issues.append({"code": "ACCESSIBILITY_UNVERIFIED", "outcome": "unknown",
                           "constraint_id": f"accessibility:{index}", "message": "No selected traveler states available to check accessibility."})
    return issues, checks
