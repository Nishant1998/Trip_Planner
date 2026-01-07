from __future__ import annotations

from enum import Enum
from datetime import UTC
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator


class Coordinates(BaseModel):
    latitude: float
    longitude: float


class MovementClass(str, Enum):
    LOCAL = "local"
    ROUTE_STAGE = "route_stage"
    INTER_BASE = "inter_base"
    INTERNATIONAL = "international"
    UNKNOWN = "unknown"


class DayKind(str, Enum):
    ARRIVAL = "arrival"
    DEPARTURE = "departure"
    SIGHTSEEING = "sightseeing"
    ROUTE = "route"
    TRANSFER = "transfer"
    TRANSFER_PLUS_SIGHTSEEING = "transfer_plus_sightseeing"
    EXCURSION = "excursion"
    REST_OR_BUFFER = "rest_or_buffer"


class TransportMode(str, Enum):
    WALK = "walk"
    DRIVE = "drive"
    TAXI = "taxi"
    RENTAL_CAR = "rental_car"
    TRANSIT = "transit"
    METRO = "metro"
    BUS = "bus"
    TRAIN = "train"
    FLIGHT = "flight"
    CYCLING = "cycling"
    UNKNOWN = "unknown"


class ModeSelectionStatus(str, Enum):
    PREFERRED = "preferred"
    ALLOWED_ALTERNATIVE = "allowed_alternative"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    NO_ALLOWED_MODE = "no_allowed_mode"
    RESEARCH_EVIDENCE_MISMATCH = "research_evidence_mismatch"


class ModeResolution(BaseModel):
    movement_class: MovementClass
    requested_modes: list[TransportMode] = Field(default_factory=list)
    preferred_mode: TransportMode | None = None
    selected_mode: TransportMode | None = None
    provider_mode: str | None = None
    selection_status: ModeSelectionStatus
    fallback_used: bool = False
    fallback_reason: str | None = None
    fallback_from: TransportMode | None = None
    fallback_to: TransportMode | None = None
    selected_mode_allowed_by_user: bool = False
    preference_satisfied: bool = False
    source: str = "unknown"
    warnings: list[str] = Field(default_factory=list)


class PlanningPOI(BaseModel):
    id: str
    google_place_id: str | None = None
    name: str
    stop_id: str
    coordinates: Coordinates
    address: str | None = None
    categories: list[str] = Field(default_factory=list)
    normalized_categories: list[str] = Field(default_factory=list)
    is_mandatory: bool = False
    requirement_ids: list[str] = Field(default_factory=list)
    priority: str = "normal"
    selection_reason: str | None = None
    minimum_visit_minutes: int | None = None
    recommended_visit_minutes: int | None = None
    maximum_visit_minutes: int | None = None
    visit_minutes: int | None = None
    visit_duration_basis: str = "unknown"
    visit_duration_confidence: str = "unknown"
    queue_or_access_minutes: int | None = None
    opening_hours: list[str] = Field(default_factory=list)
    opening_periods: list[dict[str, Any]] = Field(default_factory=list)
    closed_weekdays: list[str] = Field(default_factory=list)
    adult_entry_cost: float | None = None
    entry_currency: str | None = None
    reservation_required: bool | None = None
    booking_lead_time: str | None = None
    accessibility_notes: list[str] = Field(default_factory=list)
    restrictions: list[str] = Field(default_factory=list)
    image: dict[str, Any] | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    unresolved_fields: list[str] = Field(default_factory=list)


class PlanningStop(BaseModel):
    id: str
    google_place_id: str | None = None
    name: str
    type: str
    role: str | None = None
    provider_primary_type: str | None = None
    provider_types: list[str] = Field(default_factory=list)
    coordinates: Coordinates
    address: str | None = None
    rationale: str = ""
    # Research may map a destination/base requirement directly to a stop.
    # This is the structured alternative to comparing display names.
    requirement_ids: list[str] = Field(default_factory=list)


class MandatoryRequirement(BaseModel):
    id: str
    requirement_text: str
    requirement_type: str
    required: bool = True


class ScheduledService(BaseModel):
    """Evidence-backed service instants; offsets preserve local departure/arrival clocks."""

    departure_at: AwareDatetime
    arrival_at: AwareDatetime
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def chronological(self):
        if self.arrival_at.astimezone(UTC) <= self.departure_at.astimezone(UTC):
            raise ValueError("Service arrival must be after departure in UTC")
        return self

    @property
    def elapsed_minutes(self) -> int:
        delta = self.arrival_at.astimezone(UTC) - self.departure_at.astimezone(UTC)
        return max(1, round(delta.total_seconds() / 60))


class RouteFact(BaseModel):
    origin_id: str
    destination_id: str
    mode: str
    distance_km: float | None = None
    duration_minutes: int | None = None
    straight_line_km: float = 0
    status: str
    evidence_ids: list[str] = Field(default_factory=list)
    provenance: Literal[
        "research_verified", "research_estimated", "provider_verified", "unsupported", "unknown"
    ] = "unknown"
    mode_resolution: ModeResolution | None = None
    diagnostic_route: dict[str, Any] | None = None
    scheduled_service: ScheduledService | None = None

    @model_validator(mode="after")
    def service_duration_matches(self):
        if self.scheduled_service is not None:
            elapsed = self.scheduled_service.elapsed_minutes
            if self.duration_minutes is not None and abs(self.duration_minutes - elapsed) > 1:
                raise ValueError("Route duration conflicts with dated service evidence")
            self.duration_minutes = elapsed
        return self


class PlannerInput(BaseModel):
    source_report_path: str
    trip_name: str
    status: str
    origin: str
    end_location: str | None = None
    timezone: str | None = None
    trip_purpose: str | None = None
    travel_style: str = "moderate"
    start_date: str
    end_date: str
    duration_days: int
    currency: str
    travellers: dict[str, Any]
    destination_type: str | None = None
    trip_scope: str | None = None
    activity_interests: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    cuisine_preferences: list[str] = Field(default_factory=list)
    dietary_restrictions: list[str] = Field(default_factory=list)
    accommodation_preferences: dict[str, Any] = Field(default_factory=dict)
    mandatory_constraints: list[str] = Field(default_factory=list)
    financial_context: dict[str, Any] = Field(default_factory=dict)
    transport_preferences: dict[str, Any] = Field(default_factory=dict)
    time_preferences: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    trip_notes: list[str] = Field(default_factory=list)
    trip_context_notes: list[str] = Field(default_factory=list)
    traveler_context: dict[str, str] = Field(default_factory=dict)
    stops: list[PlanningStop]
    pois: list[PlanningPOI]
    requirements: list[MandatoryRequirement]
    routes: list[RouteFact] = Field(default_factory=list)
    # Keep the complete expected movement set at the handoff boundary.  Route
    # facts alone cannot distinguish an unsupported provider mode from a leg
    # that the researcher never considered.
    transport_requirements: list[dict[str, Any]] = Field(default_factory=list)
    # Immutable Research evidence is retained at the handoff boundary. Routing
    # may use only an explicitly normalized, endpoint/mode-matched route fact.
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    clusters: list[dict[str, Any]] = Field(default_factory=list)
    stay_bases: list[dict[str, Any]] = Field(default_factory=list)
    weather_context: list[dict[str, Any]] = Field(default_factory=list)
    current_facts: list[dict[str, Any]] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unresolved_gaps: list[str] = Field(default_factory=list)


class PlanningWorld(BaseModel):
    input: PlannerInput
    base_stop_ids: list[str]
    mandatory_poi_ids: list[str]
    optional_poi_ids: list[str]
    pois_by_stop: dict[str, list[str]]
    route_members: list[str]


class DaySkeleton(BaseModel):
    day_number: int
    base_stop_id: str | None = None
    theme: str
    kind: DayKind = DayKind.SIGHTSEEING
    candidate_poi_ids: list[str] = Field(default_factory=list)
    preserve_order: bool = False
    confirmation_required_poi_ids: list[str] = Field(default_factory=list)


class TripStrategy(BaseModel):
    base_sequence: list[str] = Field(default_factory=list)
    nights_by_base: dict[str, int] = Field(default_factory=dict)
    days: list[DaySkeleton]
    mandatory_assignments: dict[str, int] = Field(default_factory=dict)
    rationale: str = ""
    uncertainty_notes: list[str] = Field(default_factory=list)


class TransportLeg(BaseModel):
    origin_id: str
    destination_id: str
    mode: str
    start_time: str
    end_time: str
    duration_minutes: int | None = None
    buffer_minutes: int = 0
    distance_km: float | None = None
    status: str
    evidence_ids: list[str] = Field(default_factory=list)
    uncertainty: bool = False
    origin_name: str | None = None
    destination_name: str | None = None
    provenance: Literal[
        "research_verified", "research_estimated", "provider_verified", "unsupported", "unknown"
    ] = "unknown"
    critical: bool = False
    mode_resolution: ModeResolution | None = None
    scheduled_service: ScheduledService | None = None


class Activity(BaseModel):
    poi_id: str
    name: str
    start_time: str
    end_time: str
    planned_visit_minutes: int
    mandatory: bool
    requirement_ids: list[str] = Field(default_factory=list)
    address: str | None = None
    opening_hours_status: str
    notes: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    image_reference: dict[str, Any] | None = None


class ScheduleBlockType(str, Enum):
    TRAVEL = "travel"
    BUFFER = "buffer"
    WAIT = "wait"
    ACCESS = "access"
    ACTIVITY = "activity"
    MEAL = "meal"
    REST = "rest"
    UNRESOLVED_TRANSFER = "unresolved_transfer"
    FREE_TIME = "free_time"


class ScheduleBlock(BaseModel):
    """One explicit, chronological unit of a day's deterministic clock."""

    id: str
    type: ScheduleBlockType
    start_time: str
    end_time: str
    duration_minutes: int | None = None
    origin_id: str | None = None
    destination_id: str | None = None
    poi_id: str | None = None
    grounded: bool = True
    status: str = "complete"
    notes: list[str] = Field(default_factory=list)
    scheduled_service: ScheduledService | None = None


class DayPlan(BaseModel):
    day_number: int
    date: str
    title: str
    overnight_base: str | None = None
    day_start: str
    day_end: str
    start_anchor: str | None = None
    end_anchor: str | None = None
    start_anchor_id: str | None = None
    end_anchor_id: str | None = None
    day_kind: DayKind = DayKind.SIGHTSEEING
    schedule_state: Literal["complete", "partial_timing", "unresolved_transfer"] = (
        "complete"
    )
    timeline: list[ScheduleBlock] = Field(default_factory=list)
    # Optional Step-3 physical waypoints, used when a base transfer must occur
    # before sightseeing.  Step 2 validates this exact sequence.
    route_node_ids: list[str] = Field(default_factory=list)
    activities: list[Activity] = Field(default_factory=list)
    provisional_activities: list[Activity] = Field(default_factory=list)
    transport_legs: list[TransportLeg] = Field(default_factory=list)
    meals_rest: list[str] = Field(default_factory=list)
    known_cost: float = 0
    warnings: list[str] = Field(default_factory=list)
    confirm_before_visit: list[str] = Field(default_factory=list)


class RouteLeg(BaseModel):
    id: str
    origin_node_id: str
    destination_node_id: str
    origin_name: str
    destination_name: str
    movement_class: MovementClass = MovementClass.UNKNOWN
    requested_mode: TransportMode | None = None
    selected_mode: TransportMode | None = None
    provider_mode: str | None = None
    mode: str = "unknown"
    distance_km: float | None = None
    duration_minutes: int | None = None
    status: str = "unknown"
    source: str = "unknown"
    evidence_ids: list[str] = Field(default_factory=list)
    fallback_used: bool = False
    uncertainty: bool = True
    critical: bool = True
    warnings: list[str] = Field(default_factory=list)


class DayRouteChain(BaseModel):
    day_number: int
    start_anchor_id: str
    end_anchor_id: str
    day_kind: DayKind = DayKind.SIGHTSEEING
    ordered_node_ids: list[str]
    route_legs: list[RouteLeg] = Field(default_factory=list)
    structural_complete: bool = False
    resolved_leg_count: int = 0
    unknown_leg_count: int = 0
    unsupported_leg_count: int = 0
    missing_leg_count: int = 0
    warnings: list[str] = Field(default_factory=list)


class TripRoutePlan(BaseModel):
    days: list[DayRouteChain] = Field(default_factory=list)
    boundary_legs: list[RouteLeg] = Field(default_factory=list)
    inter_day_transfers: list[RouteLeg] = Field(default_factory=list)
    structural_complete: bool = False
    timing_complete: bool = False
    metrics: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class ValidationIssue(BaseModel):
    code: str
    severity: Literal["hard", "soft", "warning"]
    day_number: int | None = None
    message: str
    # Optional stable context lets Step 4 compare repeated issue codes without
    # collapsing separate POI/route failures into one set member.
    entity_ids: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)


class ReviewerFinding(BaseModel):
    """A concise, explainable observation from the one final reviewer pass."""

    finding_id: str
    category: Literal[
        "PACING",
        "DAY_BALANCE",
        "TRAVEL_PAYOFF",
        "REPETITION",
        "PREFERENCE_ALIGNMENT",
        "NIGHTLIFE_CONFLICT",
        "EXCURSION_QUALITY",
        "ACCESSIBILITY_PACING",
        "BASE_CHURN",
        "UNCERTAINTY",
        "THEME_MISMATCH",
    ]
    severity: Literal["INFO", "LOW", "MEDIUM", "HIGH"]
    day_numbers: list[int] = Field(default_factory=list)
    poi_ids: list[str] = Field(default_factory=list)
    summary: str
    actionable: bool = False
    recommended_action: str | None = None


class ReviewerEdit(BaseModel):
    """Narrow intent only: deterministic code owns every factual rebuild."""

    edit_id: str
    finding_id: str | None = None
    action: Literal[
        "MOVE_OPTIONAL_POI", "DROP_OPTIONAL_POI", "SWAP_POI_ORDER", "SET_DAY_THEME"
    ]
    poi_id: str | None = None
    from_day: int | None = None
    to_day: int | None = None
    other_poi_id: str | None = None
    new_theme: str | None = None
    rationale: str = ""


class BudgetSummary(BaseModel):
    known_attraction_cost: float = 0
    currency: str
    status: Literal["known", "partial", "unknown"] = "partial"
    categories: dict[str, dict[str, Any]] = Field(default_factory=dict)
    total_verified: float = 0
    total_estimated: float = 0
    unknown_categories: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PlanningResult(BaseModel):
    status: Literal["complete", "best_effort", "invalid_input"]
    trip_summary: str
    selected_bases: list[str]
    nights_by_base: dict[str, int]
    days: list[DayPlan]
    unscheduled_optional_pois: list[str] = Field(default_factory=list)
    satisfied_mandatory_requirements: list[str] = Field(default_factory=list)
    unsatisfied_mandatory_requirements: list[str] = Field(default_factory=list)
    relaxed_constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    budget_summary: BudgetSummary
    planning_metrics: dict[str, Any] = Field(default_factory=dict)
    trace_references: dict[str, str] = Field(default_factory=dict)
