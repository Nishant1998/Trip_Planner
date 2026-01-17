"""Bind interpreted expense quotes to dated stays, meals or individual journeys."""
from hashlib import sha256
import json


def validate_expense_scope(claim, draft):
    if draft is None:
        raise ValueError("Expense claims require the current dated draft")
    days = [day for day in draft.days if day.date.isoformat() == str(claim.applicable_date)]
    if len(days) != 1:
        raise ValueError("Expense date does not identify one draft day")
    day = days[0]
    category = claim.expense.category
    if category == "accommodation":
        if (day.overnight_kind != "stay" or day.overnight_anchor_id != claim.entity_id
                or day is draft.days[-1] or claim.expense_step_ids):
            raise ValueError("Accommodation expense must identify its overnight stay")
    else:
        steps = {step.id: step for step in day.timeline.steps} if day.timeline else {}
        selected = claim.expense_step_ids
        kind = "meal" if category == "meals" else "travel"
        if (not selected or len(selected) != len(set(selected))
                or any(key not in steps or steps[key].kind != kind for key in selected)
                or not any(claim.entity_id in {steps[key].origin_id, steps[key].destination_id}
                           for key in selected)):
            raise ValueError("Expense must identify dated meal/travel steps and a location")
    selected = set(claim.expense_step_ids)
    scope = {"date": day.date.isoformat(), "entity": claim.entity_id, "category": category,
             "stay": day.overnight_anchor_id if category == "accommodation" else None,
             "steps": [step.model_dump(mode="json") for step in day.timeline.steps
                       if step.id in selected] if day.timeline else []}
    return sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()


def expense_scope_issues(draft, claims):
    """A quote for one stay/journey is not evidence for all other days/journeys."""
    used = {item.id: item for item in draft.costs}
    scoped = [claim for claim in claims.values() if claim.get("field") == "expense"
              and claim.get("status") == "accepted" and claim.get("fact_id") in used]
    issues = []
    for claim in scoped:
        from .claims import EvidenceClaim
        try:
            raw = {key: value for key, value in claim.items() if key in EvidenceClaim.model_fields}
            if validate_expense_scope(EvidenceClaim.model_validate(raw), draft) != claim.get("expense_scope_hash"):
                raise ValueError("Expense journey changed")
        except ValueError:
            issues.append({"code": "EXPENSE_SCOPE_CHANGED", "category": used[claim["fact_id"]].category,
                           "outcome": "unknown", "message": "An expense no longer matches its dated stay or journey; obtain or rebind supporting evidence."})
    # Separate transport categories cannot hide an uncovered travel step between them.
    categories = {used[row["fact_id"]].category for row in scoped}
    for category in categories:
        rows = [row for row in scoped if used[row["fact_id"]].category == category]
        if category in {"main_transport", "local_transport", "meals"}:
            transport = [row for row in scoped if used[row["fact_id"]].category in {"main_transport", "local_transport"}]
            selected = rows if category == "meals" else transport
            kind = "meal" if category == "meals" else "travel"
            covered = {(row["applicable_date"], key) for row in selected for key in row["expense_step_ids"]}
            required = {(day.date.isoformat(), step.id) for day in draft.days if day.timeline
                        for step in day.timeline.steps if step.kind == kind}
        else:
            covered = {row["applicable_date"] for row in rows}
            required = {day.date.isoformat() for day in draft.days[:-1] if day.overnight_kind == "stay"}
        if required - covered:
            issues.append({"code": "EXPENSE_SCOPE_INCOMPLETE", "category": category,
                           "outcome": "unknown", "message": "Retrieved expense quotes cover only part of the selected stays, meal days or transport; full affordability is unconfirmed."})
    return issues
