"""Cumulative structural gates; final delivery validation remains authoritative."""
from collections import Counter
from zoneinfo import ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field

from .contracts import PhaseIssue
from .phase_checks import check_draft
from .stages import PlanningTask, TASK_ORDER
from .timeline import local_instant


class MilestoneCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: PlanningTask
    issues: list[PhaseIssue] = Field(default_factory=list)

    @property
    def can_advance(self):
        return not any(issue.outcome == "fail" for issue in self.issues)


def check_milestone(draft, data, *, task: PlanningTask, explicit_order=(),
                    registered_anchors=(), anchor_bases=None):
    """Check all structural obligations through this task, including revised days."""
    position = TASK_ORDER.index(task)
    structural = check_draft(draft, data,
        phase="skeleton" if position < 2 else "city_details",
        explicit_order=explicit_order, registered_anchors=registered_anchors,
        anchor_bases=anchor_bases)
    result = MilestoneCheck(task=task, issues=structural.issues)

    def issue(code, message, day=None, *, unknown=False):
        result.issues.append(PhaseIssue(code=code, message=message, day_number=day,
            outcome="unknown" if unknown else "fail"))

    stops = {stop.id for stop in data.stops}
    for day in draft.days:
        if day.base_stop_id is not None and day.base_stop_id not in stops:
            issue("DAY_BASE_UNKNOWN", "Day base must be a researched destination.", day.day_number)
        if position < 1:
            continue
        if day.usable_minutes is None:
            issue("USABLE_TIME_UNKNOWN", "Usable sightseeing time has not been established.", day.day_number, unknown=True)
        if day.start_anchor_id != day.end_anchor_id:
            timeline = day.timeline
            if timeline is None or not any(step.kind == "travel" for step in timeline.steps):
                issue("JOURNEY_MISSING", "Changing daily location requires explicit travel legs.", day.day_number)
            elif (timeline.start_anchor_id, timeline.end_anchor_id) != (day.start_anchor_id, day.end_anchor_id):
                issue("JOURNEY_ENDPOINTS", "Major journey endpoints must match the day.", day.day_number)
        if day.overnight_kind == "stay" and day.day_number < data.duration_days:
            if day.day_number not in draft.stay_area_by_night:
                issue("STAY_AREA_MISSING", "Record a stay area or explicit unknown for this night.", day.day_number)
            elif draft.stay_area_by_night[day.day_number] == "unknown":
                issue("STAY_AREA_UNKNOWN", "Overnight stay area remains unresolved.", day.day_number, unknown=True)
        if position < 3:
            continue
        timeline = day.timeline
        if timeline is None or not timeline.steps:
            issue("DAILY_SCHEDULE_MISSING", "Every day requires explicit activity, travel or intentional rest blocks.", day.day_number)
            continue
        if timeline.start_time.date() != day.date:
            issue("TIMELINE_DATE", "Timeline must start on its assigned date.", day.day_number)
        try:
            local_instant(timeline.start_time, timeline.timezone)
            local_instant(timeline.deadline, timeline.timezone)
        except (ValueError, ZoneInfoNotFoundError):
            issue("TIMELINE_LOCAL_TIME",
                  "Timeline timestamps must match the declared destination-local timezone.",
                  day.day_number)
        if (timeline.start_anchor_id, timeline.end_anchor_id) != (day.start_anchor_id, day.end_anchor_id):
            issue("TIMELINE_ANCHORS", "Schedule and day endpoints must agree.", day.day_number)
        selected = Counter(day.selected_poi_ids)
        visited = Counter(step.origin_id for step in timeline.steps if step.kind == "visit")
        if selected != visited:
            issue("TIMELINE_VISIT_SET", "Schedule must include exactly the allocated visits.", day.day_number)
        location = timeline.start_anchor_id
        for step in timeline.steps:
            if step.origin_id != location:
                issue("LOCATION_DISCONTINUITY", "Consecutive blocks must connect without teleportation.", day.day_number)
            location = step.destination_id
        if location != timeline.end_anchor_id:
            issue("TIMELINE_ENDPOINT", "Schedule must reach its declared endpoint.", day.day_number)
        if day.day_kind.value == "rest_or_buffer" and not day.notes:
            issue("REST_REASON", "An intentional light/rest day needs an explanation.", day.day_number)

    if position >= 4:
        if not draft.costs:
            issue("COST_LEDGER_MISSING", "Provide the cost ledger, retaining explicit unknown amounts.")
        if len({item.id for item in draft.costs}) != len(draft.costs):
            issue("DUPLICATE_COST", "Cost identities must be unique.")
    return result
