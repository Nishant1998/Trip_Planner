# Implements budget, booking-window, and cached destination-access checks.
"""Pre-summary gate checks run once before the confirmation summary is generated.

Three checks run just before Riya generates the trip summary:

  1. Budget feasibility  — dynamic internet-assisted estimate; blocks summary
                           until user confirms or revises budget.
  2. Booking window      — warns when trip starts within 3 days (incl. today);
                           informational only, never blocks.
  3. Accessibility       — verified hard closures / mandatory restrictions only;
                           blocks summary until user confirms or adjusts dates.

Checks 1 and 3 are async (internet lookups) and run in parallel.
Check 2 is instant.

Re-run behaviour
----------------
Once a user accepts a blocking check ("proceed anyway"), the check is skipped
unless the fields it depends on change:
  - Budget check  : re-runs if trip_budget / num_adults / num_children /
                    num_seniors / trip_duration_days change.
  - Accessibility : re-runs if destination or start_date / end_date change.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date

from app.collection.intelligence import maybe_build_destination_intelligence
from app.collection.prompts.feasibility import build_feasibility_task_prompt
from app.exceptions import AgentRunError
from app.models.memory import PreSummaryGate, TripMemory

logger = logging.getLogger(__name__)

_MIN_INTERNATIONAL_BUDGET_PER_TRAVELLER_INR = 40_000


# ── Data shapes ──────────────────────────────────────────────────────────────


@dataclass
class PreSummaryIssue:
    """One check result that has something to communicate to the user."""

    check_name: str  # "budget" | "booking_window" | "accessibility"
    message: str  # User-facing text.
    blocks_summary: bool  # True = soft-block; False = informational note.


@dataclass
class PreSummaryCheckResult:
    """Aggregate result of all pre-summary checks for one turn."""

    issues: list[PreSummaryIssue] = field(default_factory=list)

    @property
    def has_blocking_issues(self) -> bool:
        return any(issue.blocks_summary for issue in self.issues)

    def combined_user_message(self, *, proceed_hint: bool = True) -> str:
        """Build one combined user-facing message from all collected issues."""

        blocking = [i for i in self.issues if i.blocks_summary]
        notes = [i for i in self.issues if not i.blocks_summary]

        parts: list[str] = [i.message for i in blocking]
        if notes:
            parts.extend(i.message for i in notes)

        combined = "\n\n".join(parts)
        if blocking and proceed_hint:
            combined += "\n\nWant to make adjustments, or should I go ahead and plan within what you have?"
        return combined


# ── Snapshot helpers (for re-run detection) ───────────────────────────────────


def _budget_snapshot(memory: TripMemory) -> str:
    """Return an MD5 of the fields that affect budget feasibility."""

    fields = {
        "trip_budget": memory.needed.trip_budget,
        "num_adults": memory.needed.num_adults,
        "num_children": memory.optional.num_children,
        "num_seniors": memory.optional.num_seniors,
        "trip_duration_days": memory.needed.trip_duration_days,
        "origin": memory.needed.origin,
        "destination": (
            memory.needed.destination.items if memory.needed.destination else []
        ),
    }
    return hashlib.md5(
        json.dumps(fields, default=str, sort_keys=True).encode()
    ).hexdigest()


def _total_travellers(memory: TripMemory) -> int:
    return (
        (memory.needed.num_adults or 0)
        + (memory.optional.num_children or 0)
        + (memory.optional.num_seniors or 0)
    )


def check_obvious_budget_feasibility(memory: TripMemory) -> PreSummaryIssue | None:
    """Catch only unmistakably impossible INR international budgets early.

    This is a conservative safety floor, not a price quote or a replacement for
    the evidence-backed estimate at confirmation.
    """

    budget = memory.needed.trip_budget
    people = _total_travellers(memory)
    if (
        budget is None
        or people <= 0
        or memory.metadata.currency_code != "INR"
        or str(memory.metadata.trip_scope or "").casefold() != "international"
    ):
        return None
    minimum = people * _MIN_INTERNATIONAL_BUDGET_PER_TRAVELLER_INR
    if budget >= minimum:
        return None
    return PreSummaryIssue(
        check_name="budget",
        message=(
            f"A total budget of ₹{budget:,} is not enough for an international trip "
            f"for {people} traveller(s), even before day-to-day spending. Please raise "
            f"the budget or choose a closer trip before we continue."
        ),
        blocks_summary=True,
    )


def check_selected_transport_feasibility(memory: TripMemory) -> PreSummaryIssue | None:
    """Stop only a researched, physically impossible main-route choice."""

    research = memory.internal.sanity_state.destination_intelligence or {}
    if research.get("transport_should_block") is not True:
        return None
    warning = research.get("transport_warning")
    if not isinstance(warning, str) or not warning.strip():
        warning = (
            "The selected transport cannot complete this route in practice. "
            "Please choose a workable travel option before we continue."
        )
    return PreSummaryIssue(
        check_name="transport",
        message=warning.strip(),
        blocks_summary=True,
    )


def run_early_trip_sanity_checks(memory: TripMemory) -> list[PreSummaryIssue]:
    """Return immediate, non-negotiable route/budget conflicts once known."""

    return [
        issue
        for issue in (
            check_selected_transport_feasibility(memory),
            check_obvious_budget_feasibility(memory),
        )
        if issue is not None
    ]


def _accessibility_snapshot(memory: TripMemory) -> str:
    """Return an MD5 of the fields that affect destination accessibility."""

    dest = memory.needed.destination
    fields = {
        "destination": dest.items if dest else [],
        "start_date": str(memory.needed.start_date),
        "end_date": str(memory.needed.end_date),
    }
    return hashlib.md5(
        json.dumps(fields, default=str, sort_keys=True).encode()
    ).hexdigest()


# ── Individual checks ─────────────────────────────────────────────────────────


def check_booking_window(memory: TripMemory) -> PreSummaryIssue | None:
    """Warn (never block) when the trip starts within 3 days including today."""

    start = memory.needed.start_date
    if not start:
        return None

    days_until = (start - date.today()).days

    if days_until <= 0:
        timing = "today"
    elif days_until == 1:
        timing = "tomorrow"
    elif days_until == 2:
        timing = "in 2 days"
    else:
        return None  # More than 2 days away — no warning needed.

    return PreSummaryIssue(
        check_name="booking_window",
        message=(
            f"Just so you know — your trip starts {timing}. "
            "Transport and hotel availability may be limited at this stage; "
            "I'll plan with whatever is available."
        ),
        blocks_summary=False,
    )


async def check_budget_feasibility(
    memory: TripMemory,
    feasibility_agent,
) -> PreSummaryIssue | None:
    """Check if trip_budget is plausible for the group, route, and duration.

    Uses the feasibility agent (internet-aware) to estimate a realistic cost
    range and compares it against the stated budget.  Skipped when the user
    has already accepted and the relevant fields have not changed.
    """

    gate: PreSummaryGate = memory.internal.sanity_state.pre_summary_gate

    budget = memory.needed.trip_budget
    currency_code = memory.metadata.currency_code
    num_adults = memory.needed.num_adults
    duration = memory.needed.trip_duration_days
    destination = memory.needed.destination
    origin = memory.needed.origin

    if not all([budget, num_adults, duration, destination, origin]):
        return None

    # This legacy feasibility response is denominated in INR. Comparing it to
    # a budget in another currency would create a false warning, so defer that
    # comparison until the evidence-backed multi-currency planner runs.
    if currency_code != "INR":
        return None

    # Skip when accepted and no relevant field has changed.
    current_snapshot = _budget_snapshot(memory)
    if gate.budget_check_accepted and gate.budget_fields_snapshot == current_snapshot:
        return None

    total_people = _total_travellers(memory)

    # A conservative floor catches obviously impossible INR budgets even when
    # the optional feasibility model is unavailable or returns too little
    # evidence. It is never presented as a quote or an exact price.
    obvious_issue = check_obvious_budget_feasibility(memory)
    if obvious_issue is not None:
        return obvious_issue

    try:
        estimate = await feasibility_agent.run(
            build_feasibility_task_prompt(
                destination=destination.items,
                trip_scope=memory.metadata.trip_scope or "domestic",
                num_adults=memory.needed.num_adults or 1,
                trip_duration_days=duration,
                trip_budget=budget,
                num_children=memory.optional.num_children or 0,
                num_seniors=memory.optional.num_seniors or 0,
                travel_style=(
                    memory.optional.travel_style.value
                    if memory.optional.travel_style
                    else None
                ),
                spending_tier=(
                    memory.optional.spending_tier.value
                    if memory.optional.spending_tier
                    else None
                ),
                long_distance_modes=(
                    [mode.value for mode in memory.optional.long_distance_modes]
                    if memory.optional.long_distance_modes
                    else None
                ),
                hotel_tier=(
                    memory.optional.hotel_tier.value
                    if memory.optional.hotel_tier
                    else None
                ),
            )
        )

        if budget >= estimate.min_budget_inr:
            return None  # Budget is sufficient — no issue.

        shortfall = estimate.min_budget_inr - budget
        person_label = "person" if total_people == 1 else "people"
        dest_str = ", ".join(destination.items)

        message = (
            f"A trip like this — {total_people} {person_label}, "
            f"{origin} to {dest_str}, {duration} days — "
            f"typically costs around ₹{estimate.min_budget_inr:,} to ₹{estimate.max_budget_inr:,}. "
            f"Your budget is ₹{budget:,}, which is about ₹{shortfall:,} below the realistic minimum."
        )
        if estimate.reasoning:
            message += f" ({estimate.reasoning})"

        return PreSummaryIssue(
            check_name="budget",
            message=message,
            blocks_summary=True,
        )

    except AgentRunError as exc:
        logger.warning("Budget feasibility check failed, skipping: %s", exc)
        return None


async def check_destination_accessibility(
    memory: TripMemory,
) -> PreSummaryIssue | None:
    """Reuse cached research for verified closures or legal restrictions.

    Only blocks when the agent returns a high-confidence confirmed closure.
    Uncertain results, weather concerns, and availability issues are ignored.
    Skipped when the user has already accepted and destination/dates unchanged.
    """

    gate: PreSummaryGate = memory.internal.sanity_state.pre_summary_gate

    destination = memory.needed.destination
    start = memory.needed.start_date

    if not destination or not destination.items or not start:
        return None

    # Skip when accepted and no relevant field has changed.
    current_snapshot = _accessibility_snapshot(memory)
    if (
        gate.accessibility_check_accepted
        and gate.accessibility_fields_snapshot == current_snapshot
    ):
        return None

    research = memory.internal.sanity_state.destination_intelligence or {}
    if research.get("access_should_block") is True:
        warning = research.get("access_warning")
        if isinstance(warning, str) and warning.strip():
            return PreSummaryIssue(
                check_name="accessibility",
                message=warning.strip(),
                blocks_summary=True,
            )

    return None


# ── Orchestration entry point ─────────────────────────────────────────────────


async def run_pre_summary_checks(
    memory: TripMemory,
    agents,
) -> PreSummaryCheckResult:
    """Run all three pre-summary checks and return a combined result.

    Checks 1 and 3 run in parallel (both are async internet lookups).
    Check 2 is instant and always included.
    """

    # Run the distinct budget estimate while ensuring the one shared destination
    # research result exists. A failed earlier lookup gets one retry here.
    budget_issue, _ = await asyncio.gather(
        check_budget_feasibility(memory, agents.feasibility),
        maybe_build_destination_intelligence(memory, agents),
    )
    accessibility_issue = await check_destination_accessibility(memory)

    # Instant deterministic check.
    booking_issue = check_booking_window(memory)

    issues: list[PreSummaryIssue] = []

    # Blocking issues first so they read naturally before the warning note.
    if budget_issue:
        issues.append(budget_issue)
    if accessibility_issue:
        issues.append(accessibility_issue)
    if booking_issue:
        issues.append(booking_issue)

    # Cache the booking-window note in memory so the summary can include it
    # even when the gate doesn't fire (no blocking issues).
    gate = memory.internal.sanity_state.pre_summary_gate
    gate.booking_window_note = booking_issue.message if booking_issue else None

    return PreSummaryCheckResult(issues=issues)


# ── Gate state mutators ───────────────────────────────────────────────────────


def accept_pre_summary_checks(
    memory: TripMemory, result: PreSummaryCheckResult
) -> None:
    """Mark all blocking checks in *result* as accepted and store field snapshots.

    Call this when the user says "proceed" without making any field changes.
    """

    gate = memory.internal.sanity_state.pre_summary_gate

    for issue in result.issues:
        if issue.check_name == "budget" and issue.blocks_summary:
            gate.budget_check_accepted = True
            gate.budget_fields_snapshot = _budget_snapshot(memory)
        elif issue.check_name == "accessibility" and issue.blocks_summary:
            gate.accessibility_check_accepted = True
            gate.accessibility_fields_snapshot = _accessibility_snapshot(memory)

    gate.gate_awaiting_response = False
