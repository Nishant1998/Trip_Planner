"""Internal schedule produced by the feasibility engine before rendering."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.constraints import AppliedRelaxation, RelaxationAttempt
from app.models.duration import VisitDuration
from app.models.evidence import GeoPoint, RouteEvidence
from app.models.route import TravelWindow


class ScheduledTravel(BaseModel):
    kind: Literal["transport"] = "transport"
    start: datetime
    end: datetime
    route: RouteEvidence
    window: TravelWindow
    origin_location: GeoPoint | None = None
    destination_location: GeoPoint | None = None


class ScheduledVisit(BaseModel):
    kind: Literal["activity"] = "activity"
    start: datetime
    end: datetime
    place_id: str
    duration: VisitDuration


class ScheduledBreak(BaseModel):
    kind: Literal["meal", "rest", "check_in", "check_out", "free_time"]
    start: datetime
    end: datetime
    title: str
    description: str


ScheduledItem = ScheduledTravel | ScheduledVisit | ScheduledBreak


class CandidateDay(BaseModel):
    day_number: int = Field(ge=1)
    date: date
    start_location: str
    base_stop: str
    items: list[ScheduledItem] = Field(default_factory=list)


class FeasibleCandidate(BaseModel):
    candidate_id: str
    days: list[CandidateDay]
    covered_stops: list[str] = Field(default_factory=list)
    visited_place_ids: list[str] = Field(default_factory=list)
    omitted_optional_place_ids: list[str] = Field(default_factory=list)
    relaxations: list[AppliedRelaxation] = Field(default_factory=list)
    preference_score: int = Field(default=0, ge=0)


class CandidateFailure(BaseModel):
    issue_codes: list[str]
    message: str
    affected_ids: list[str] = Field(default_factory=list)


class CandidateSearchResult(BaseModel):
    candidate: FeasibleCandidate | None = None
    failure: CandidateFailure | None = None
    attempts: list[RelaxationAttempt] = Field(default_factory=list)

    @property
    def feasible(self) -> bool:
        return self.candidate is not None
