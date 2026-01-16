"""Per-day chronological simulation with explicit uncertainty and no I/O."""
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ..models.planning import ScheduledService


class Window(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: datetime
    end: datetime
    last_admission: datetime | None = None


class TimelineStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    kind: Literal["visit", "travel", "meal", "rest", "buffer", "check_in", "check_out", "free_time"]
    origin_id: str
    destination_id: str
    minutes: int | None = Field(default=None, ge=0, le=10080)
    minimum_minutes: int | None = Field(default=None, gt=0, le=10080)
    basis: Literal["evidence", "estimate", "planning_allowance", "unknown"]
    windows: list[Window] = Field(default_factory=list, max_length=32)
    availability_known: bool = False
    session_required: bool = False
    session_confirmed: bool = False
    fixed_start: datetime | None = None
    scheduled_service: ScheduledService | None = None

    @model_validator(mode="after")
    def local_step(self):
        if self.kind != "travel" and self.origin_id != self.destination_id:
            raise ValueError("Non-travel blocks cannot change traveler location")
        if self.basis == "unknown" and self.minutes is not None:
            raise ValueError("Unknown duration must remain null")
        if self.scheduled_service is not None:
            if self.kind != "travel" or self.minutes != self.scheduled_service.elapsed_minutes:
                raise ValueError("Service must be travel with its evidence-backed duration")
            if self.fixed_start is not None and self.fixed_start != self.scheduled_service.departure_at:
                raise ValueError("Service departure cannot be changed")
        return self


class TimelineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timezone: str
    start_time: datetime
    deadline: datetime
    start_anchor_id: str
    end_anchor_id: str
    steps: list[TimelineStep] = Field(max_length=128)


def local_instant(value: datetime, timezone: str) -> datetime:
    """Reject ambiguous/nonexistent wall clocks unless an explicit valid offset is supplied."""
    zone = ZoneInfo(timezone)
    if value.tzinfo is not None:
        actual = value.astimezone(zone)
        if actual.replace(tzinfo=None) != value.replace(tzinfo=None) or actual.utcoffset() != value.utcoffset():
            raise ValueError("Timestamp offset does not match the specified local timezone")
        return value.astimezone(UTC)
    candidates = set()
    for fold in (0, 1):
        instant = value.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) == value:
            candidates.add(instant)
    if len(candidates) != 1:
        raise ValueError("Ambiguous or nonexistent local time; supply a valid explicit offset")
    return candidates.pop()


def check_timeline(request: TimelineRequest) -> dict:
    zone = ZoneInfo(request.timezone)
    current = local_instant(request.start_time, request.timezone)
    deadline = local_instant(request.deadline, request.timezone)
    if deadline <= current:
        raise ValueError("Deadline must follow starting instant")
    if len({step.id for step in request.steps}) != len(request.steps):
        raise ValueError("Duplicate timeline step identity")
    location = request.start_anchor_id
    result, issues = [], []
    estimated = False

    def add(code, step, outcome="fail"):
        issues.append({"code": code, "step_id": step.id, "outcome": outcome})

    for step in request.steps:
        if location != step.origin_id:
            add("LOCATION_DISCONTINUITY", step)
        if step.minimum_minutes and step.minutes is not None and step.minutes < step.minimum_minutes:
            add("VISIT_MINIMUM", step)
        if step.kind == "visit" and not step.availability_known:
            add("AVAILABILITY_UNKNOWN", step, "unknown")
        if step.kind == "visit" and step.availability_known and not step.windows:
            add("CLOSED", step)
        if step.session_required and not step.session_confirmed:
            add("SESSION_UNCONFIRMED", step, "unknown")
        estimated = estimated or step.basis in {"estimate", "planning_allowance"}
        earliest = current
        service = step.scheduled_service
        if current is not None and (step.fixed_start is not None or service is not None):
            fixed = service.departure_at.astimezone(UTC) if service else local_instant(step.fixed_start, request.timezone)
            if current > fixed:
                add("MISSED_FIXED_START", step)
            current = max(current, fixed)
        if step.minutes is None:
            add("DURATION_UNKNOWN", step, "unknown")
            current = None
        if current is not None and step.windows:
            fits = []
            for window in step.windows:
                opening = local_instant(window.start, request.timezone)
                closing = local_instant(window.end, request.timezone)
                if closing <= opening:
                    raise ValueError("Opening window end must follow start")
                beginning = max(current, opening)
                admission = local_instant(window.last_admission, request.timezone) if window.last_admission else closing
                if beginning <= admission and beginning + timedelta(minutes=step.minutes) <= closing:
                    fits.append(beginning)
            if fits:
                current = min(fits)
                if step.fixed_start is not None and current != local_instant(step.fixed_start, request.timezone):
                    add("FIXED_START_OUTSIDE_WINDOW", step)
            else:
                add("NO_FITTING_WINDOW", step)
        begin = current
        if current is not None:
            current = (service.arrival_at.astimezone(UTC) if service and begin == service.departure_at.astimezone(UTC)
                       else current + timedelta(minutes=step.minutes))
        start_zone = service.departure_at.tzinfo if service else zone
        end_zone = service.arrival_at.tzinfo if service else zone
        result.append({"id": step.id, "start": begin.astimezone(start_zone).isoformat() if begin else None,
            "end": current.astimezone(end_zone).isoformat() if current else None,
            "wait_minutes": int((begin-earliest).total_seconds()/60) if begin and earliest else None,
            "basis": step.basis, "timing_estimated": estimated,
            "origin_id": step.origin_id, "destination_id": step.destination_id})
        location = step.destination_id
    if location != request.end_anchor_id:
        issues.append({"code": "WRONG_ENDPOINT", "outcome": "fail"})
    if current is not None and current > deadline:
        issues.append({"code": "DEADLINE_EXCEEDED", "outcome": "fail"})
    outcome = "fail" if any(i["outcome"] == "fail" for i in issues) else "unknown" if issues else "pass"
    return {"outcome": outcome, "timeline": result, "issues": issues,
            "end_anchor_id": location, "slack_minutes": (deadline-current).total_seconds()/60 if current else None,
            "timing_estimated": estimated}
