"""Small deterministic checks for provider-backed weekly opening periods."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from app.models.evidence import PlaceEvidence, WeeklyOpeningPeriod

WEEK_MINUTES = 7 * 24 * 60


def visit_is_within_opening_hours(
    place: PlaceEvidence,
    start: datetime,
    end: datetime,
) -> bool | None:
    """Return None when the provider supplied no machine-readable hours."""
    if not place.opening_periods:
        return None
    start_minute = _week_minute(start.weekday(), start.time())
    duration = round((end - start).total_seconds() / 60)
    end_minute = start_minute + max(0, duration)
    return any(
        _period_contains(period, start_minute, end_minute)
        for period in place.opening_periods
    )


def hours_text_for_date(place: PlaceEvidence, visit_date: date) -> str | None:
    """Select the provider's human-readable hours for the itinerary day."""
    if len(place.weekday_hours) == 7:
        return place.weekday_hours[visit_date.weekday()]
    return place.opening_hours


def next_valid_visit_start(
    place: PlaceEvidence,
    earliest: datetime,
    duration_minutes: int,
    latest_end: datetime,
    last_entry_minutes_before_close: int | None = None,
) -> datetime | None:
    """Find the earliest same-day start that fits known provider hours.

    When hours are unavailable, return a provisional earliest start rather
    than pretending the place is closed.  Validation marks that visit with a
    traveller-facing confirmation warning.
    """
    if not place.opening_periods:
        candidate = earliest.replace(second=0, microsecond=0)
        return (
            candidate
            if candidate + timedelta(minutes=duration_minutes) <= latest_end
            else None
        )
    candidate = earliest.replace(second=0, microsecond=0)
    duration = timedelta(minutes=duration_minutes)
    while candidate + duration <= latest_end:
        if visit_is_within_opening_hours(
            place,
            candidate,
            candidate + duration,
        ) and visit_respects_last_entry(
            place,
            candidate,
            last_entry_minutes_before_close,
        ):
            return candidate
        candidate += timedelta(minutes=1)
    return None


def visit_respects_last_entry(
    place: PlaceEvidence,
    start: datetime,
    margin_minutes: int | None,
) -> bool:
    """Apply a last-entry margin only when research supplied one."""

    if margin_minutes is None or not place.opening_periods:
        return True
    visit_start = _week_minute(start.weekday(), start.time())
    for period in place.opening_periods:
        if period.close_day is None or period.close_time is None:
            return True
        opening = _week_minute(period.open_day, period.open_time)
        closing = _week_minute(period.close_day, period.close_time)
        if closing <= opening:
            closing += WEEK_MINUTES
        for shift in (-WEEK_MINUTES, 0, WEEK_MINUTES):
            shifted_open = opening + shift
            shifted_close = closing + shift
            if shifted_open <= visit_start <= shifted_close - margin_minutes:
                return True
    return False


def _period_contains(
    period: WeeklyOpeningPeriod,
    visit_start: int,
    visit_end: int,
) -> bool:
    if period.close_day is None or period.close_time is None:
        return True
    opening = _week_minute(period.open_day, period.open_time)
    closing = _week_minute(period.close_day, period.close_time)
    if closing <= opening:
        closing += WEEK_MINUTES
    return any(
        opening + shift <= visit_start and visit_end <= closing + shift
        for shift in (-WEEK_MINUTES, 0, WEEK_MINUTES)
    )


def _week_minute(day: int, value: time) -> int:
    return day * 24 * 60 + value.hour * 60 + value.minute
