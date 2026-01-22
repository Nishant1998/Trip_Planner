"""Version-bound reviewer judgments; never overrides deterministic validation."""
from .contracts import fingerprint


def check_hard_review(candidate, data, utilities):
    review = utilities.constraint_review
    current = (review.get("draft_hash") == fingerprint(candidate.draft)
               and review.get("evidence_revision") == candidate.evidence_revision
               and review.get("input_fingerprint") == candidate.input_fingerprint)
    assessments = review.get("assessments", []) if current else []
    days = {day.day_number for day in candidate.draft.days}
    evidence = {row["id"] for row in data.evidence if row.get("id")}
    evidence.update(row["source_id"] for row in utilities.claims.values()
                    if row.get("status") == "accepted" and row.get("source_id"))
    issues, checks = [], []
    for index, text in enumerate(data.mandatory_constraints):
        rows = [row for row in assessments if row.get("constraint_index") == index]
        row = rows[0] if len(rows) == 1 else {}
        valid = (row.get("source_text") == text and set(row.get("day_numbers", [])) == days and bool(days)
                 and set(row.get("evidence_ids", [])) <= evidence
                 and (row.get("basis") == "itinerary_decision" or bool(row.get("evidence_ids"))))
        outcome = "fail" if valid and row.get("outcome") == "violated" else (
            "pass" if valid and row.get("outcome") == "supported" else "unknown")
        record = {"constraint_id": f"hard:{index}", "outcome": outcome,
                  "basis": "reviewer_judgment" if valid else "missing_or_inapplicable_review",
                  "independently_verified": False, "explanation": row.get("explanation") if valid else None,
                  "evidence_ids": row.get("evidence_ids", []) if valid else []}
        checks.append(record)
        if outcome != "pass":
            issues.append({**record, "code": "HARD_CONSTRAINT_VIOLATED" if outcome == "fail" else "HARD_CONSTRAINT_REVIEW",
                           "message": f"Review all trip days and every clause of the confirmed requirement: {text}"})
    return issues, checks
