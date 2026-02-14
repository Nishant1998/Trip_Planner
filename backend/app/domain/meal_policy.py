"""Deterministic meal labels and realistic meal-time windows."""

from __future__ import annotations

from datetime import datetime, time

_MEAL_WINDOWS = {
    "breakfast": (time(6, 30), time(10, 30), time(8, 0)),
    "lunch": (time(11, 30), time(15, 30), time(12, 30)),
    "dinner": (time(17, 30), time(22, 0), time(19, 0)),
}


def meal_label(kind: str, title: str) -> str | None:
    normalized_title = title.casefold()
    for label in _MEAL_WINDOWS:
        if label in normalized_title:
            return label
    if kind == "meal" or " meal" in f" {normalized_title}":
        return "meal"
    return None


def meal_problem(kind: str, title: str, start: time) -> tuple[str, str] | None:
    label = meal_label(kind, title)
    if label is None:
        return None
    if kind != "meal":
        return (
            "MEAL_KIND_INVALID",
            f"{title!r} is a meal but is not structured as a meal block.",
        )
    if label in _MEAL_WINDOWS:
        earliest, latest, _preferred = _MEAL_WINDOWS[label]
        if not earliest <= start <= latest:
            return (
                "MEAL_TIME_INVALID",
                f"{title!r} starts outside the supported {label} time window.",
            )
    return None


def align_meal_start(kind: str, title: str, current: datetime) -> datetime:
    """Delay an early meal to its preferred time; never move time backwards."""
    label = meal_label(kind, title)
    if kind != "meal" or label not in _MEAL_WINDOWS:
        return current
    _earliest, _latest, preferred = _MEAL_WINDOWS[label]
    preferred_start = datetime.combine(current.date(), preferred)
    return max(current, preferred_start)
