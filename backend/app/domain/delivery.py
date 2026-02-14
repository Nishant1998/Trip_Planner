"""Final fail-closed gate between internal planning and user delivery."""

from __future__ import annotations

from app.models.plan import DeliveryStatus, PlanStatus, TripPlan
from app.exceptions import PlannerFinalReviewError

_CRITICAL_PROVIDERS = {"places", "routes", "llm"}


def require_deliverable_plan(plan: TripPlan, *, mode: str) -> TripPlan:
    """Return only a plan that is safe for the UI to render."""
    # A draft is explicitly unverified and carries its own warnings. It is a
    # safe recovery result, not a plan the UI may present as fully ready.
    if plan.delivery_status is DeliveryStatus.ADJUSTED_DRAFT:
        return plan
    blocking_codes = [issue.code for issue in plan.blocking_issues]
    if (
        blocking_codes
        or plan.validation is None
        or not plan.validation.hard_constraints_passed
        or plan.status is PlanStatus.INFEASIBLE
    ):
        codes = ", ".join(blocking_codes) or "HARD_CONSTRAINT_CHECK_FAILED"
        raise PlannerFinalReviewError(f"Final review blocked: {codes}")

    if mode != "live":
        return plan

    acceptable_statuses = {"ok"}
    unavailable = {
        name: plan.generation.provider_status.get(name, "missing")
        for name in _CRITICAL_PROVIDERS
        if plan.generation.provider_status.get(name)
        not in (acceptable_statuses | ({"estimated"} if name == "routes" else set()))
    }
    if unavailable:
        summary = ", ".join(f"{name}={status}" for name, status in unavailable.items())
        raise PlannerFinalReviewError(f"Critical evidence unavailable: {summary}")

    # A disclosed partial itinerary is safe to deliver, but it must keep its
    # partial status so the UI never claims every requested stop was planned.
    return plan
