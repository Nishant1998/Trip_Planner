"""Deterministic final checks before a collected trip can be confirmed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.models.memory import TripMemory

REQUIRED_FIELD_NAMES = (
    "origin",
    "destination",
    "start_date",
    "end_date",
    "num_adults",
    "trip_budget",
)


@dataclass(frozen=True, slots=True)
class ReadinessIssue:
    """One user-facing detail that prevents safe trip planning."""

    field_name: str
    message: str


@dataclass(frozen=True, slots=True)
class CollectionReadiness:
    """Outcome of the non-AI collection gate."""

    issues: tuple[ReadinessIssue, ...] = ()

    @property
    def is_ready(self) -> bool:
        return not self.issues

    @property
    def blocking_fields(self) -> list[str]:
        """Return distinct field names in the order the user should fix them."""

        return list(dict.fromkeys(issue.field_name for issue in self.issues))

    def user_message(self) -> str:
        """Explain the first blocking detail in plain language."""

        if self.is_ready:
            return "Your basic trip details are ready for confirmation."
        return f"Before I can plan this trip, {self.issues[0].message}"


def assess_collection_readiness(
    memory: TripMemory,
    *,
    today: date | None = None,
) -> CollectionReadiness:
    """Check essential trip details without using an AI decision.

    This gate intentionally checks only facts that can be decided from the
    saved record: a usable origin/destination, workable dates, traveller
    details, practical daily hours, and a positive budget. External travel
    research is performed separately and can add its own blocker before the
    user is asked to confirm.
    """

    issues: list[ReadinessIssue] = []
    current_day = today or date.today()

    origin = memory.needed.origin
    if not _is_place_like(origin):
        issues.append(ReadinessIssue("origin", "please tell me where the trip starts."))

    destination = memory.needed.destination
    if destination is None or destination.type.value == "open":
        issues.append(
            ReadinessIssue("destination", "please choose a destination first.")
        )
    elif not destination.items or any(
        not _is_place_like(item) for item in destination.items
    ):
        issues.append(
            ReadinessIssue(
                "destination",
                "please provide a clear destination with at least one place name.",
            )
        )

    start_date = memory.needed.start_date
    end_date = memory.needed.end_date
    if start_date is None:
        issues.append(ReadinessIssue("start_date", "please provide a start date."))
    elif start_date < current_day:
        issues.append(
            ReadinessIssue("start_date", "the start date is already in the past.")
        )

    if end_date is None:
        issues.append(ReadinessIssue("end_date", "please provide an end date."))
    elif end_date < current_day:
        issues.append(
            ReadinessIssue("end_date", "the end date is already in the past.")
        )

    if start_date is not None and end_date is not None:
        if end_date <= start_date:
            issues.append(
                ReadinessIssue(
                    "end_date",
                    "the end date needs to be after the start date.",
                )
            )
        else:
            expected_days = (end_date - start_date).days + 1
            duration = memory.needed.trip_duration_days
            if duration is not None and duration != expected_days:
                issues.append(
                    ReadinessIssue(
                        "trip_duration_days",
                        "the trip length does not match the selected dates.",
                    )
                )

    adults = memory.needed.num_adults
    children = memory.optional.num_children or 0
    seniors = memory.optional.num_seniors or 0
    if adults is None or adults < 0:
        issues.append(
            ReadinessIssue(
                "num_adults", "please provide a valid adult traveller count."
            )
        )
    if children < 0:
        issues.append(
            ReadinessIssue(
                "num_children", "the child traveller count cannot be negative."
            )
        )
    if seniors < 0:
        issues.append(
            ReadinessIssue(
                "num_seniors", "the senior traveller count cannot be negative."
            )
        )
    if (adults or 0) + seniors <= 0:
        issues.append(
            ReadinessIssue(
                "num_adults",
                "at least one adult or senior traveller needs to be included.",
            )
        )

    children_ages = memory.optional.children_ages
    if children_ages is not None and len(children_ages) != children:
        issues.append(
            ReadinessIssue(
                "children_ages",
                "the number of child ages needs to match the child traveller count.",
            )
        )

    day_start_time = memory.optional.day_start_time
    day_end_time = memory.optional.day_end_time
    if (
        day_start_time is not None
        and day_end_time is not None
        and day_end_time <= day_start_time
    ):
        issues.append(
            ReadinessIssue(
                "day_end_time",
                "the planned day needs to end after it starts.",
            )
        )

    budget = memory.needed.trip_budget
    if budget is None or budget <= 0:
        issues.append(
            ReadinessIssue("trip_budget", "please provide a positive trip budget.")
        )

    return CollectionReadiness(tuple(issues))


def get_missing_required_fields(memory: TripMemory) -> list[str]:
    """Return essential fields that are missing or need correction."""

    return assess_collection_readiness(memory).blocking_fields


def is_collection_complete(memory: TripMemory) -> bool:
    """Return whether every deterministic collection check passes."""

    return not get_missing_required_fields(memory)


def _is_place_like(value: object) -> bool:
    """Reject blank or non-place-like text while accepting all writing systems."""

    return (
        isinstance(value, str)
        and bool(value.strip())
        and any(character.isalpha() for character in value)
    )
