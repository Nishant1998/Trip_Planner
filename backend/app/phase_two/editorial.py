"""Apply optional narrative to existing IDs; keep the validated schedule intact."""
from .agent_io import PresentationOutput
from .contracts import PlannedDay


def apply_editorial_copy(
    days: list[PlannedDay], copy: PresentationOutput,
) -> list[PlannedDay]:
    result = [day.model_copy(deep=True) for day in days]
    by_day = {day.day: day for day in result}
    seen_days = set()
    for draft in copy.days:
        if draft.day not in by_day or draft.day in seen_days:
            raise ValueError("Narrative references an unknown or repeated day")
        seen_days.add(draft.day)
        day = by_day[draft.day]
        events = {event.id: event for event in day.events}
        seen_events = set()
        for item in draft.events:
            if item.event_id not in events or item.event_id in seen_events:
                raise ValueError("Narrative references an unknown or repeated event")
            seen_events.add(item.event_id)
            events[item.event_id].reason = item.description
        day.title, day.summary = draft.title, draft.summary
    return result
