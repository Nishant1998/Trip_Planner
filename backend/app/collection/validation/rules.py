"""Runs fast rule-based checks on extracted fields before memory writes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable

from pydantic import TypeAdapter

from app.models.api import ExtractedFieldItem
from app.models.enums import (
    FlightCabin,
    HotelTier,
    LodgingType,
    RoomType,
    SpendingTier,
    TimeWindow,
    TrainClass,
    TransportLocal,
    TransportLong,
    TravelStyle,
    TripPurpose,
)

_DATE_FMT = "%Y-%m-%d"
_TIME_FMT = "%H:%M"
MAX_YEARS_FUTURE = 5
MAX_TRIP_DURATION_DAYS = 365
MAX_LOCATION_TEXT_LENGTH = 240
MIN_BUDGET = 500
MAX_ADULTS = 100
MAX_CHILDREN_SENIORS = 50
MAX_CHILD_AGE = 17

VALID_DESTINATION_TYPES = {"city", "cities", "region", "route", "poi", "open"}

ENUM_OPTIONS: dict[str, list[str]] = {
    "trip_purpose": [item.value for item in TripPurpose],
    "depart_time_window": [item.value for item in TimeWindow],
    "arrival_time_window": [item.value for item in TimeWindow],
    "return_time_window": [item.value for item in TimeWindow],
    "travel_style": [item.value for item in TravelStyle],
    "spending_tier": [item.value for item in SpendingTier],
    "local_transport_modes": [item.value for item in TransportLocal],
    "long_distance_modes": [item.value for item in TransportLong],
    "train_class": [item.value for item in TrainClass],
    "flight_cabin_class": [item.value for item in FlightCabin],
    "hotel_tier": [item.value for item in HotelTier],
    "lodging_type": [item.value for item in LodgingType],
    "room_type_pref": [item.value for item in RoomType],
}

ENUM_LIST_FIELDS = {"local_transport_modes", "long_distance_modes"}

STRING_LIST_FIELDS = {
    "activity_interests",
    "cuisine_prefs",
    "dietary_restrictions",
    "accessibility_needs",
    "trip_notes",
    "avoid",
}

AGENT_PLACE_CHECK_FIELDS = {"origin", "end_location", "destination"}
REASKABLE_FAILURE_FIELDS = {
    "origin",
    "destination",
    "start_date",
    "end_date",
    "trip_duration_days",
    "num_adults",
    "trip_budget",
    "end_location",
    "trip_purpose",
    "num_children",
    "children_ages",
    "num_seniors",
    "depart_time_window",
    "arrival_time_window",
    "return_time_window",
    "day_start_time",
    "day_end_time",
    "travel_style",
    "spending_tier",
    "local_transport_modes",
    "long_distance_modes",
    "train_class",
    "flight_cabin_class",
    "activity_interests",
    "must_see",
    "avoid",
    "cuisine_prefs",
    "dietary_restrictions",
    "accessibility_needs",
    "hotel_tier",
    "lodging_type",
    "room_type_pref",
    "trip_notes",
}


@dataclass
class FieldCheckFailure:
    field_name: str
    rejected_value: Any
    reason: str


def _fail(field_name: str, value: Any, reason: str) -> FieldCheckFailure:
    return FieldCheckFailure(field_name=field_name, rejected_value=value, reason=reason)


def _parse_date(value: Any) -> date | None:
    """Parse a date from either a date object or a YYYY-MM-DD string."""
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), _DATE_FMT).date()
    except (ValueError, TypeError):
        return None


def _parse_time(value: Any) -> bool:
    try:
        datetime.strptime(str(value), _TIME_FMT)
        return True
    except (ValueError, TypeError):
        return False


def _is_blank_string(value: Any) -> bool:
    return not isinstance(value, str) or not value.strip()


def _has_letters(value: str) -> bool:
    """Recognize letters from every Unicode writing system."""

    return any(character.isalpha() for character in value)


def _check_location_string(field_name: str, value: Any) -> FieldCheckFailure | None:
    """Applies to: origin, end_location."""

    if _is_blank_string(value):
        return _fail(field_name, value, "Value is empty or blank.")
    if not _has_letters(value):
        return _fail(
            field_name,
            value,
            "Value contains no letters - looks like garbage input.",
        )
    if len(value) > MAX_LOCATION_TEXT_LENGTH:
        return _fail(field_name, value, "Place text is unusually long.")
    return None


def _check_destination(field_name: str, value: Any) -> FieldCheckFailure | None:
    """Applies to: destination object {type, items[]}."""

    if not isinstance(value, dict):
        return _fail(
            field_name, value, "Destination must be an object with 'type' and 'items'."
        )
    destination_type = value.get("type")
    if destination_type not in VALID_DESTINATION_TYPES:
        return _fail(
            field_name,
            value,
            f"Destination type '{destination_type}' is not valid.",
        )
    items = value.get("items")
    if not isinstance(items, list):
        return _fail(field_name, value, "Destination items must be a list.")
    if destination_type != "open" and not items:
        return _fail(field_name, value, "Destination items list is empty.")
    for item in items:
        if _is_blank_string(item):
            return _fail(
                field_name, value, f"Destination item '{item}' is blank or empty."
            )
        if not _has_letters(item):
            return _fail(
                field_name, value, f"Destination item '{item}' has no letters."
            )
        if len(item) > MAX_LOCATION_TEXT_LENGTH:
            return _fail(field_name, value, "A destination name is unusually long.")
    return None


def _check_positive_int(
    field_name: str, value: Any, max_val: int
) -> FieldCheckFailure | None:
    """Applies to: num_adults, trip_duration_days. Zero is not allowed."""

    if not isinstance(value, int) or isinstance(value, bool):
        return _fail(field_name, value, "Value must be a whole number.")
    if value <= 0:
        return _fail(field_name, value, "Value must be greater than zero.")
    if value > max_val:
        return _fail(
            field_name,
            value,
            f"Value {value} is unreasonably large (max {max_val}).",
        )
    return None


def _check_non_negative_int(
    field_name: str,
    value: Any,
    max_val: int,
) -> FieldCheckFailure | None:
    """Applies to: num_children, num_seniors. Zero is valid."""

    if not isinstance(value, int) or isinstance(value, bool):
        return _fail(field_name, value, "Value must be a whole number.")
    if value < 0:
        return _fail(field_name, value, "Value cannot be negative.")
    if value > max_val:
        return _fail(
            field_name,
            value,
            f"Value {value} is unreasonably large (max {max_val}).",
        )
    return None


def _check_enum(field_name: str, value: Any) -> FieldCheckFailure | None:
    """Applies to all single-value enum fields."""

    options = ENUM_OPTIONS.get(field_name, [])
    if value not in options:
        return _fail(
            field_name, value, f"'{value}' is not a valid option. Valid: {options}"
        )
    return None


def _check_enum_list(field_name: str, value: Any) -> FieldCheckFailure | None:
    """Applies to: local_transport_modes, long_distance_modes."""

    options = ENUM_OPTIONS.get(field_name, [])
    if not isinstance(value, list) or not value:
        return _fail(field_name, value, "Value must be a non-empty list.")
    for item in value:
        if item not in options:
            return _fail(
                field_name, value, f"'{item}' is not a valid option. Valid: {options}"
            )
    return None


def _check_date(field_name: str, value: Any) -> FieldCheckFailure | None:
    """Applies to: start_date, end_date."""

    parsed = _parse_date(value)
    if parsed is None:
        return _fail(field_name, value, "Date must be in YYYY-MM-DD format.")
    today = date.today()
    if parsed < today:
        return _fail(field_name, value, "Date is in the past.")
    # Switches the future-date check to a year-comparison so a date like 2031-01-01 never slips through in early months of the limit year.
    # Reject any date whose calendar year is >= (current year + MAX_YEARS_FUTURE).
    # This catches dates like 2031-01-01 when the limit is 5 years from 2026
    # without relying on same-month precision, which avoids false passes in early months.
    if parsed.year >= today.year + MAX_YEARS_FUTURE:
        return _fail(
            field_name,
            value,
            (
                f"Date is more than {MAX_YEARS_FUTURE} years in the future"
                " - likely a typo."
            ),
        )
    return None


def _check_time(field_name: str, value: Any) -> FieldCheckFailure | None:
    """Applies to: day_start_time, day_end_time."""

    if not _parse_time(value):
        return _fail(field_name, value, "Time must be in HH:MM format.")
    return None


def _check_money(field_name: str, value: Any) -> FieldCheckFailure | None:
    """Applies to: trip_budget."""

    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return _fail(field_name, value, "Budget must be a positive number.")
    if value < MIN_BUDGET:
        return _fail(
            field_name,
            value,
            f"Budget of {value} is too low to fund any real travel (min {MIN_BUDGET}).",
        )
    return None


def _check_string_list(field_name: str, value: Any) -> FieldCheckFailure | None:
    """Applies to plain string list fields."""

    if not isinstance(value, list):
        return _fail(field_name, value, "Value must be a list.")
    for item in value:
        if _is_blank_string(item):
            return _fail(field_name, value, "List contains a blank or empty item.")
    return None


def _check_children_ages(field_name: str, value: Any) -> FieldCheckFailure | None:
    """Applies to: children_ages."""

    if not isinstance(value, list):
        return _fail(field_name, value, "Children ages must be a list.")
    for age in value:
        if not isinstance(age, int) or isinstance(age, bool):
            return _fail(field_name, value, f"Age '{age}' is not a whole number.")
        if age < 0:
            return _fail(field_name, value, f"Age {age} cannot be negative.")
        if age > MAX_CHILD_AGE:
            return _fail(
                field_name,
                value,
                (
                    f"Age {age} is over {MAX_CHILD_AGE} - this traveler would be "
                    "counted as an adult."
                ),
            )
    return None


_FIELD_CHECKER: dict[str, Callable[[str, Any], FieldCheckFailure | None]] = {
    "origin": _check_location_string,
    "end_location": _check_location_string,
    "destination": _check_destination,
    "start_date": _check_date,
    "end_date": _check_date,
    "day_start_time": _check_time,
    "day_end_time": _check_time,
    "trip_budget": _check_money,
    "num_adults": lambda f, v: _check_positive_int(f, v, MAX_ADULTS),
    "trip_duration_days": lambda f, v: _check_positive_int(
        f,
        v,
        MAX_TRIP_DURATION_DAYS,
    ),
    "num_children": lambda f, v: _check_non_negative_int(
        f,
        v,
        MAX_CHILDREN_SENIORS,
    ),
    "num_seniors": lambda f, v: _check_non_negative_int(
        f,
        v,
        MAX_CHILDREN_SENIORS,
    ),
    "children_ages": _check_children_ages,
    "must_see": _check_string_list,
    "avoid": _check_string_list,
    "activity_interests": _check_string_list,
    "cuisine_prefs": _check_string_list,
    "dietary_restrictions": _check_string_list,
    "accessibility_needs": _check_string_list,
    "trip_notes": _check_string_list,
}

for _field in ENUM_OPTIONS:
    if _field not in ENUM_LIST_FIELDS:
        _FIELD_CHECKER[_field] = _check_enum

for _field in ENUM_LIST_FIELDS:
    _FIELD_CHECKER[_field] = _check_enum_list


def build_quick_check_failure_payload(
    failures: list[FieldCheckFailure],
) -> list[dict[str, Any]]:
    """Return a JSON-ready rejection payload for the response agent."""

    return [
        {
            "field_name": failure.field_name,
            "rejected_value": TypeAdapter(Any).dump_python(
                failure.rejected_value,
                mode="json",
            ),
            "reason": failure.reason,
            "ask_user_again": failure.field_name in REASKABLE_FAILURE_FIELDS,
        }
        for failure in failures
    ]


def run_quick_checks(
    extracted_fields: list[ExtractedFieldItem],
) -> tuple[list[FieldCheckFailure], set[str]]:
    """Check each extracted value by itself before it can be saved.

    Relationships between values already in memory (for example, whether the
    end date is after the start date) intentionally belong to the final
    collection-readiness gate. That keeps ordinary conversation turns fast and
    allows a user to supply trip details in any natural order.

    Returns
    -------
    failures
        All fields that failed and should not be written to memory.
    agent_check_fields
        Field names that passed Python checks but still need verification-agent
        place checks.
    """

    failures: list[FieldCheckFailure] = []
    passed_field_names: set[str] = set()

    for extracted_field in extracted_fields:
        field_name = extracted_field.field_name
        value = extracted_field.value
        checker = _FIELD_CHECKER.get(field_name)
        if checker is None:
            passed_field_names.add(field_name)
            continue
        failure = checker(field_name, value)
        if failure:
            failures.append(failure)
        else:
            passed_field_names.add(field_name)

    agent_check_fields = {
        field_name
        for field_name in passed_field_names
        if field_name in AGENT_PLACE_CHECK_FIELDS
    }

    return failures, agent_check_fields
