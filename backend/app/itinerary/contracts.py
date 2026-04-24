"""Versioned public plan and bounded shared-state records for itinerary planning.

All provider facts may be unknown. A complete JSON shape does not imply that
every booking price, timetable, opening period, or route is verified.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from datetime import date as Date
from enum import StrEnum
from typing import Any, Literal, TypedDict

from app.models.memory import OptionalFieldValues
from app.planning_tools.contracts import (
    BookingOption,
    BusinessStatus,
    Coordinates,
    Photo,
    PlacePhotoReference,
    RouteLeg,
    TransportOption,
    WeatherReport,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator


class FactState(StrEnum):
    VERIFIED = "verified"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


class AgentTaskState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    PARTIAL = "partial"
    SKIPPED = "skipped"


class Evidence(BaseModel):
    provider: str
    retrieved_at: datetime | None = None
    reference: str | None = None
    status: FactState


class PlanWarning(BaseModel):
    code: str
    message: str
    source: str
    severity: Literal["info", "warning", "error"] = "warning"
    affected_id: str | None = None


class TripRequest(BaseModel):
    """Read-only normalized view plus an immutable serialized trip-detail collection snapshot."""

    model_config = ConfigDict(frozen=True)
    preferences: OptionalFieldValues = Field(default_factory=OptionalFieldValues)
    preference_sources: dict[str, str | None] = Field(default_factory=dict)
    destination_type: str | None = None
    destination_subtype: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    trip_id: int
    snapshot_id: str
    origin: str = Field(min_length=1)
    destinations: list[str] = Field(min_length=1)
    start_date: date
    end_date: date
    adults: int = Field(ge=1)
    children: int = Field(default=0, ge=0)
    budget: float | None = Field(default=None, ge=0)
    currency: str | None = None
    timezone: str | None = None
    must_see: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    accessibility_needs: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    confirmed_memory_json: str

    @model_validator(mode="after")
    def dates_in_order(self) -> TripRequest:
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        return self


class RunMeta(BaseModel):
    planning_profile: Literal["full", "daywise", "discovery"] = "full"
    run_id: str
    trip_id: int
    snapshot_id: str
    started_at: datetime
    deadline_at: datetime
    schema_version: Literal["trip_plan.v2"] = "trip_plan.v2"

    @model_validator(mode="after")
    def deadline_follows_start(self) -> RunMeta:
        if self.deadline_at <= self.started_at:
            raise ValueError("deadline must follow run start")
        return self


class DestinationRecord(BaseModel):
    id: str
    label: str
    location: Coordinates | None = None
    timezone: str | None = None
    status: FactState = FactState.UNAVAILABLE
    evidence: list[Evidence] = Field(default_factory=list)


class PlaceRecord(BaseModel):
    id: str
    destination_id: str
    name: str
    selection_reason: str = ""  # Editorial context, not a provider-verified fact.
    location: Coordinates | None = None
    address: str | None = None
    categories: list[str] = Field(default_factory=list)
    opening_hours: dict[str, Any] | None = None
    business_status: BusinessStatus | None = None
    image_refs: list[str] = Field(default_factory=list)
    photos: list[PlacePhotoReference] = Field(default_factory=list)
    display_photos: list[Photo] = Field(default_factory=list)
    map_url: str | None = None
    estimated_cost: float | None = Field(default=None, ge=0)
    currency: str | None = None
    status: FactState = FactState.UNAVAILABLE
    evidence: list[Evidence] = Field(default_factory=list)


class WeatherRecord(BaseModel):
    destination_id: str
    date: Date | None = None
    report: WeatherReport | None = None
    warnings: list[str] = Field(default_factory=list)
    min_c: float | None = None
    max_c: float | None = None
    precipitation_probability: int | None = Field(default=None, ge=0, le=100)
    status: FactState = FactState.UNAVAILABLE
    evidence: list[Evidence] = Field(default_factory=list)


class StayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    destination_id: str
    check_in: date
    check_out: date
    adults: int = Field(ge=1)
    children: int = Field(default=0, ge=0)
    rooms: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def dates_in_order(self) -> StayRequest:
        if self.check_out <= self.check_in:
            raise ValueError("check_out must follow check_in")
        return self


class AccommodationRecord(BaseModel):
    stay_request: StayRequest
    selected: BookingOption | None = None
    alternatives: list[BookingOption] = Field(default_factory=list)
    status: FactState = FactState.UNAVAILABLE
    evidence: list[Evidence] = Field(default_factory=list)
    reason: str = ""


class TransportRecord(BaseModel):
    id: str
    origin: str
    destination: str
    mode: str
    selected: TransportOption | None = None
    alternatives: list[TransportOption] = Field(default_factory=list)
    reason: str = ""
    status: FactState = FactState.UNAVAILABLE
    evidence: list[Evidence] = Field(default_factory=list)


class CostRecord(BaseModel):
    currency: str | None = None
    known_subtotal: float = Field(default=0, ge=0)
    category_totals: dict[str, float] = Field(default_factory=dict)
    unknown_components: list[str] = Field(default_factory=list)
    budget_status: Literal["within", "over", "unknown"] = "unknown"


class PlannedEvent(BaseModel):
    reason: str = ""
    id: str
    kind: Literal[
        "activity", "travel", "meal", "rest", "check_in", "check_out", "unallocated"
    ]
    title: str
    period: Literal["morning", "midday", "afternoon", "evening", "flexible"] | None = None
    start: datetime | None = None
    end: datetime | None = None
    timing_status: Literal["scheduled", "provisional", "unscheduled"] = "unscheduled"
    place_id: str | None = None
    location: Coordinates | None = None
    address: str | None = None
    opening_hours: dict[str, Any] | None = None
    image_refs: list[str] = Field(default_factory=list)
    photos: list[PlacePhotoReference] = Field(default_factory=list)
    display_photos: list[Photo] = Field(default_factory=list)
    map_url: str | None = None
    visit_minutes: int | None = Field(default=None, ge=0)
    duration_basis: Literal["provider", "planning_allowance", "unknown"] = "unknown"
    travel_from_previous: RouteLeg | None = None
    estimated_cost: float | None = Field(default=None, ge=0)
    currency: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def timing_is_coherent(self) -> PlannedEvent:
        if self.timing_status == "scheduled" and (
            self.start is None or self.end is None
        ):
            raise ValueError("scheduled event requires start and end")
        if self.start and self.end and self.end <= self.start:
            raise ValueError("event end must follow start")
        return self


class PlannedDay(BaseModel):
    workload: dict[str, Any] | None = None
    day: int = Field(ge=1)
    date: date
    base_location: str | None = None
    title: str = ""
    summary: str = ""
    events: list[PlannedEvent] = Field(default_factory=list)
    weather: WeatherRecord | None = None
    daily_cost: CostRecord = Field(default_factory=CostRecord)
    daily_distance_m: int | None = Field(default=None, ge=0)


class TripSummary(BaseModel):
    title: str
    start_date: date
    end_date: date
    adults: int
    children: int
    requested_destinations: list[str]
    origin: str
    budget: float | None = None
    currency: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    overview: str = ""


class BudgetGuidance(BaseModel):
    scope: Literal["full_trip_party"] = "full_trip_party"
    currency: str
    budget: float = Field(gt=0)
    adults: int
    children: int
    nights: int
    category_limits: dict[str, float]
    basis: str


class TripPlanV2(BaseModel):
    local_tips: list[str] = Field(default_factory=list)
    packing_list: list[str] = Field(default_factory=list)
    planning_profile: Literal["full", "daywise", "discovery"] = "full"
    route_profiles: dict[str, Any] = Field(default_factory=dict)
    schema_version: Literal["trip_plan.v2"] = "trip_plan.v2"
    plan_id: str
    trip_id: int
    run_id: str
    status: Literal["complete", "partial"]
    trip_summary: TripSummary
    destinations: list[DestinationRecord]
    accommodation: list[AccommodationRecord]
    transport: list[TransportRecord]
    days: list[PlannedDay]
    total_estimated_cost: CostRecord
    budget_guidance: BudgetGuidance | None = None
    warnings: list[PlanWarning]
    generated_at: datetime
    weather: list[WeatherRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def covers_all_dates(self) -> TripPlanV2:
        expected = (self.trip_summary.end_date - self.trip_summary.start_date).days + 1
        if len(self.days) != expected:
            raise ValueError("plan must include every requested day")
        if [day.day for day in self.days] != list(range(1, expected + 1)):
            raise ValueError("day numbers must be consecutive")
        if any(
            day.date != self.trip_summary.start_date + timedelta(days=index)
            for index, day in enumerate(self.days)
        ):
            raise ValueError("day dates must cover the requested range")
        return self


class AgentProgress(BaseModel):
    status: AgentTaskState = AgentTaskState.PENDING
    attempts: int = Field(default=0, ge=0)
    input_fingerprint: str | None = None


class RoutingState(BaseModel):
    next_agent: str | None = None
    task: str | None = None
    delegation_count: int = Field(default=0, ge=0)
    supervisor_decisions: int = Field(default=0, ge=0)
    completion_reason: str | None = None
    reason: str = ""
    repair_started: bool = False
    repair_stage: Literal["none", "specialist", "schedule", "done"] = "none"


class JourneyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    origin: str
    destination: str
    departure_date: date
    mode: Literal["flight", "train", "bus", "car"]
    departure_time: datetime | None = None


class RouteRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin_id: str
    destination_id: str
    mode: Literal["driving", "walking", "bicycling", "transit"] = "driving"


class ValidationIssue(BaseModel):
    code: str
    message: str
    affected_id: str | None = None
    owner: Literal[
        "research", "allocation", "accommodation", "transport", "schedule"
    ] = "schedule"
    severity: Literal["warning", "error"] = "error"


class TripState(TypedDict):
    route_profiles: dict[str, Any]
    run: RunMeta
    user_preferences: TripRequest
    destinations: list[DestinationRecord]
    places: list[PlaceRecord]
    weather: list[WeatherRecord]
    stay_requests: list[StayRequest]
    hotels: list[AccommodationRecord]
    transport_options: list[TransportRecord]
    route_matrix: list[RouteLeg]
    itinerary: list[PlannedDay]
    costs: CostRecord
    warnings: list[PlanWarning]
    agent_status: dict[str, AgentProgress]
    routing: RoutingState
    final_plan: TripPlanV2 | None
    persistence: dict[str, Any]
    journeys: list[JourneyRequest]
    route_requirements: list[RouteRequirement]
    validation: list[ValidationIssue]
    pending_jobs: dict[str, Any]
