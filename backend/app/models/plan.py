"""Versioned machine-readable trip-plan response."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from app.models.agent import ToolCallAudit
from app.models.constraints import AppliedRelaxation
from app.models.duration import VisitDuration
from app.models.evidence import FactStatus, GeoPoint, SourceRef
from app.models.presentation import TripPresentation
from app.models.route import TravelWindow


class PlanStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    INFEASIBLE = "infeasible"


class DeliveryStatus(StrEnum):
    """V3 public readiness without breaking the existing status field."""

    READY = "ready"
    READY_WITH_RELAXATIONS = "ready_with_relaxations"
    # The strict planner could not verify every detail, but the user still
    # receives a useful, explicitly estimated itinerary rather than an error.
    ADJUSTED_DRAFT = "adjusted_draft"


class MoneyRange(BaseModel):
    minimum: Decimal
    maximum: Decimal
    currency_code: str
    status: FactStatus
    basis: str
    source_ids: list[str] = Field(default_factory=list)


class PlaceRef(BaseModel):
    id: str
    provider_place_id: str | None = None
    name: str
    stop_name: str | None = None
    address: str | None = None
    image_url: str | None = None
    google_maps_url: str | None = None
    opening_hours: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    status: FactStatus
    hours_status: FactStatus = FactStatus.UNAVAILABLE
    source_ids: list[str] = Field(default_factory=list)


class TravelLeg(BaseModel):
    origin_label: str
    destination_label: str
    origin_location: GeoPoint | None = None
    destination_location: GeoPoint | None = None
    mode: str
    distance_km: float | None = None
    duration_minutes: int | None = None
    travel_window: TravelWindow | None = None
    status: FactStatus
    source_ids: list[str] = Field(default_factory=list)


class WeatherSummary(BaseModel):
    summary: str
    minimum_temperature_c: float | None = None
    maximum_temperature_c: float | None = None
    precipitation_probability: int | None = None
    status: FactStatus
    source_ids: list[str] = Field(default_factory=list)


class PlanItem(BaseModel):
    id: str
    kind: Literal[
        "transport",
        "activity",
        "meal",
        "rest",
        "check_in",
        "check_out",
        "free_time",
    ]
    start_time: time
    end_time: time
    start_datetime: datetime | None = None
    end_datetime: datetime | None = None
    title: str
    description: str
    place: PlaceRef | None = None
    travel_from_previous: TravelLeg | None = None
    visit_duration: VisitDuration | None = None
    estimated_cost: MoneyRange | None = None
    source_ids: list[str] = Field(default_factory=list)


class DayPlan(BaseModel):
    day_number: int
    date: date
    base_stop: str
    theme: str
    weather: WeatherSummary | None = None
    items: list[PlanItem]
    daily_cost: MoneyRange


class StaySuggestion(BaseModel):
    stop: str
    area: str
    nights: int
    description: str
    estimated_cost: MoneyRange
    status: FactStatus
    source_ids: list[str] = Field(default_factory=list)


class BudgetSummary(BaseModel):
    total_budget: Decimal
    estimated_minimum: Decimal
    estimated_maximum: Decimal
    contingency_reserved: Decimal
    currency_code: str
    within_budget: bool
    category_totals: dict[str, MoneyRange] = Field(default_factory=dict)


class ValidationIssue(BaseModel):
    code: str
    severity: Literal["warning", "blocking"]
    message: str
    day_number: int | None = None
    item_id: str | None = None


class ValidationStatus(StrEnum):
    """The deterministic validator's final decision for an itinerary."""

    APPROVED = "approved"
    APPROVED_WITH_WARNINGS = "approved_with_warnings"
    REJECTED = "rejected"


class ValidationReport(BaseModel):
    """Deterministic validation facts; intentionally contains no numeric score."""

    hard_constraints_passed: bool
    issues: list[ValidationIssue] = Field(default_factory=list)


class ValidationResult(BaseModel):
    """A stable, presentation-safe outcome of the deterministic quality gate."""

    status: ValidationStatus
    hard_constraints_passed: bool
    warnings: list[ValidationIssue] = Field(default_factory=list)
    blocking_issues: list[ValidationIssue] = Field(default_factory=list)


class GenerationMetadata(BaseModel):
    planner_version: str
    model: str
    generated_at: datetime
    request_id: str
    repair_attempts: int
    provider_status: dict[str, str] = Field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_llm_cost_usd: Decimal = Decimal("0")
    stage_latency_ms: dict[str, int] = Field(default_factory=dict)
    agent_tool_calls: list[ToolCallAudit] = Field(default_factory=list)


class TripIntelligenceItem(BaseModel):
    """One short, source-linked preparation fact; never a changed requirement."""

    topic: str
    message: str
    source_ids: list[str] = Field(default_factory=list)


class TripIntelligenceBriefing(BaseModel):
    """Compact pre-trip briefing built from the run's existing current evidence."""

    status: Literal["available", "limited"] = "limited"
    items: list[TripIntelligenceItem] = Field(default_factory=list, max_length=10)


class TripPlan(BaseModel):
    schema_version: Literal["1.0", "1.1", "1.2", "1.3", "1.4"] = "1.3"
    plan_id: str
    trip_id: str | None = None
    status: PlanStatus
    delivery_status: DeliveryStatus = DeliveryStatus.READY
    title: str
    summary: str
    start_date: date
    end_date: date
    resolved_stops: list[str]
    days: list[DayPlan]
    stays: list[StaySuggestion] = Field(default_factory=list)
    budget: BudgetSummary
    context_notes_applied: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    intelligence_briefing: TripIntelligenceBriefing | None = None
    blocking_issues: list[ValidationIssue] = Field(default_factory=list)
    validation: ValidationResult | None = None
    presentation: TripPresentation | None = None
    relaxations: list[AppliedRelaxation] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    generation: GenerationMetadata
