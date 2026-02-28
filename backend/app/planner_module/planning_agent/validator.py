"""Thin Planner V2 loop decision; deterministic runtime owns all factual checks."""

from __future__ import annotations


def outcome(candidate: dict | None, repair_count: int, max_repairs: int) -> dict:
    if candidate is None:
        return {"outcome": "repair", "issues": [{"code": "NO_SUBMISSION", "severity": "repair", "message": "Submit through submit_itinerary."}]}
    validation = candidate["validation"]
    hard = validation.get("hard_issue_count", 0)
    raw_issues = candidate.get("issues", [])
    issues = []
    for issue in raw_issues:
        severity = getattr(issue, "severity", None) or issue.get("severity", "warning")
        if severity != "hard":
            continue
        code = getattr(issue, "code", None) or issue.get("code", "VALIDATION_ISSUE")
        message = getattr(issue, "message", None) or issue.get("message", "Deterministic validation failed.")
        day_number = getattr(issue, "day_number", None) if not isinstance(issue, dict) else issue.get("day_number")
        entity_ids = getattr(issue, "entity_ids", []) if not isinstance(issue, dict) else issue.get("entity_ids", [])
        requirement_ids = getattr(issue, "requirement_ids", []) if not isinstance(issue, dict) else issue.get("requirement_ids", [])
        repairability = "external_unknown" if code in {"CRITICAL_ROUTE_UNKNOWN", "UNSUPPORTED_MODE", "NO_ALLOWED_MODE"} else "semantic_plan"
        issues.append({
            "code": code, "severity": "hard", "repairability": repairability,
            "day_number": day_number, "entity_ids": entity_ids or [],
            "requirement_ids": requirement_ids or [], "message": message,
            "suggested_actions": [] if repairability != "semantic_plan" else ["move_poi", "drop_optional_poi", "update_checkpoint"],
        })
    if hard and repair_count < max_repairs:
        semantic = [item for item in issues if item["repairability"] == "semantic_plan"]
        if semantic:
            return {"outcome": "repair", "issues": semantic}
    return {"outcome": "pass", "issues": []}
