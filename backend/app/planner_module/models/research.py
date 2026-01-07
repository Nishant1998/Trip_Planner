"""Machine-first contracts for the Research → Planner handoff."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

DestinationType = Literal["city", "cities", "country", "region", "route", "poi", "open"]
ResearchStatus = Literal["complete", "partial", "blocked"]
EvidenceKind = Literal["provider", "web", "seasonal", "assumption"]


class Destination(BaseModel):
    type: DestinationType
    items: list[str] = Field(min_length=1)
    subtype: str | None = None

    @model_validator(mode="after")
    def destination_items_are_meaningful(self) -> Destination:
        if any(not item.strip() for item in self.items):
            raise ValueError("destination items cannot be blank.")
        return self


class Travellers(BaseModel):
    num_adults: int = Field(ge=1)
    num_children: int = Field(default=0, ge=0)
    children_ages: list[int] = Field(default_factory=list)
    num_seniors: int = Field(default=0, ge=0)
    accessibility_needs: list[str] = Field(default_factory=list)


class TimePreferences(BaseModel):
    depart_time_window: str | None = None
    arrival_time_window: str | None = None
    return_time_window: str | None = None
    preferred_day_start: str | None = None
    preferred_day_end: str | None = None


class Budget(BaseModel):
    total: float | None = Field(default=None, ge=0)
    currency: str = "INR"
    spending_tier: str | None = None


class TransportPreferences(BaseModel):
    local_modes: list[str] = Field(default_factory=list)
    long_distance_modes: list[str] = Field(default_factory=list)
    train_class: str | None = None
    flight_cabin_class: str | None = None


class AccommodationPreferences(BaseModel):
    hotel_tier: str | None = None
    lodging_type: str | None = None
    room_type: str | None = None


class ResearchInput(BaseModel):
    origin: str
    destination: Destination
    start_date: date
    end_date: date
    duration_days: int = Field(ge=1)
    end_location: str | None = None
    trip_scope: str | None = None
    timezone: str | None = None
    currency_code: str = "INR"
    travellers: Travellers
    trip_purpose: str | None = None
    activity_interests: list[str] = Field(default_factory=list)
    must_see: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    cuisine_preferences: list[str] = Field(default_factory=list)
    dietary_restrictions: list[str] = Field(default_factory=list)
    travel_style: str = "moderate"
    time_preferences: TimePreferences = Field(default_factory=TimePreferences)
    budget: Budget = Field(default_factory=Budget)
    transport_preferences: TransportPreferences = Field(
        default_factory=TransportPreferences
    )
    accommodation_preferences: AccommodationPreferences = Field(
        default_factory=AccommodationPreferences
    )
    trip_notes: list[str] = Field(default_factory=list)
    trip_context_notes: list[str] = Field(default_factory=list)
    traveler_context: dict[str, str] = Field(default_factory=dict)
    destination_context: dict[str, Any] = Field(default_factory=dict)
    field_sources: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def dates_match_duration(self) -> ResearchInput:
        if not self.origin.strip():
            raise ValueError("origin cannot be blank.")
        expected = (self.end_date - self.start_date).days + 1
        if self.duration_days != expected:
            raise ValueError("duration_days must include both start_date and end_date.")
        return self


class Evidence(BaseModel):
    id: str
    kind: EvidenceKind
    provider: str
    subject: str
    url: str | None = None
    retrieved_value: dict[str, Any] = Field(default_factory=dict)


class Coordinates(BaseModel):
    latitude: float
    longitude: float


class ResolvedStop(BaseModel):
    id: str
    google_place_id: str | None = None
    name: str
    type: str
    # ``type`` is retained for existing serialized reports.  V2 separates the
    # semantic role selected by the researcher from factual provider taxonomy.
    role: Literal[
        "base", "route_member", "poi_destination", "excursion", "terminal"
    ] | None = None
    # Semantic parent selected by the research agent, when this stop is a POI
    # destination rather than an overnight/sightseeing base.
    parent_stop_ref: str | None = None
    provider_primary_type: str | None = None
    provider_types: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    coordinates: Coordinates
    address: str | None = None
    rationale: str
    confidence: Literal["high", "medium", "low"]
    evidence_ids: list[str] = Field(min_length=1)


class MandatoryRequirement(BaseModel):
    """A user promise with validation rules appropriate to its meaning."""

    id: str
    requirement_text: str
    requirement_type: Literal[
        "destination_or_base", "exact_poi", "poi_or_experience", "all_route_members"
    ]
    required: bool = True


class POI(BaseModel):
    id: str
    google_place_id: str | None = None
    name: str
    stop_id: str
    coordinates: Coordinates
    address: str | None = None
    categories: list[str] = Field(default_factory=list)
    normalized_categories: list[str] = Field(default_factory=list)
    image: dict[str, Any] | None = None
    priority: Literal["must", "high", "normal"] = "normal"
    is_mandatory: bool = False
    requirement_ids: list[str] = Field(default_factory=list)
    selection_reason: str | None = None
    relevance: str
    business_status: str | None = None
    is_recommendable: bool = True
    visit_minutes: int | None = Field(default=None, ge=1)
    visit_duration_basis: Literal[
        "official",
        "provider",
        "source_reported",
        "source_estimate",
        "corroborated_estimate",
        "estimated_from_poi_characteristics",
        "unknown",
    ] = "unknown"
    visit_duration_confidence: Literal["high", "medium", "low", "unknown"] = "unknown"
    minimum_visit_minutes: int | None = Field(default=None, ge=1)
    recommended_visit_minutes: int | None = Field(default=None, ge=1)
    maximum_visit_minutes: int | None = Field(default=None, ge=1)
    queue_or_access_minutes: int | None = Field(default=None, ge=0)
    opening_hours: list[str] = Field(default_factory=list)
    opening_periods: list[dict[str, Any]] = Field(default_factory=list)
    closed_weekdays: list[str] = Field(default_factory=list)
    date_specific_status: Literal["open", "closed", "unknown"] = "unknown"
    special_closure_notes: list[str] = Field(default_factory=list)
    last_entry_time: str | None = None
    adult_entry_cost: float | None = Field(default=None, ge=0)
    entry_currency: str | None = None
    reservation_required: bool | None = None
    booking_lead_time: str | None = None
    accessibility_notes: list[str] = Field(default_factory=list)
    restrictions: list[str] = Field(default_factory=list)
    photography_restrictions: list[str] = Field(default_factory=list)
    crowd_notes: list[str] = Field(default_factory=list)
    weather_sensitive: bool | None = None
    indoor_outdoor: Literal["indoor", "outdoor", "mixed", "unknown"] = "unknown"
    evidence_ids: list[str] = Field(default_factory=list)
    unresolved_fields: list[str] = Field(default_factory=list)


class GeographicCluster(BaseModel):
    id: str
    stop_id: str
    name: str
    poi_ids: list[str] = Field(min_length=1)
    centroid: Coordinates
    rationale: str


class RouteEvidence(BaseModel):
    origin_id: str
    destination_id: str
    mode: str
    relevant_modes: list[str] = Field(default_factory=list)
    preferred_modes: list[str] = Field(default_factory=list)
    distance_km: float | None = Field(default=None, ge=0)
    duration_minutes: int | None = Field(default=None, ge=0)
    straight_line_km: float = Field(ge=0)
    status: Literal["verified", "unavailable", "unsupported_by_provider"]
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def verified_routes_need_evidence(self) -> RouteEvidence:
        if self.status == "verified" and (
            not self.evidence_ids or self.duration_minutes is None
        ):
            raise ValueError("A verified route needs duration and provider evidence.")
        return self


class TransportRequirement(BaseModel):
    """One deterministic movement the researcher must account for.

    ``unsupported_by_provider`` is an explicit, truthful limitation rather than
    a missing leg.  It lets the planner decide whether it can obtain the timing
    through its own compatible routing source without pretending that ORS can
    provide transit or flight data.
    """

    id: str
    leg_kind: Literal["entry", "intercity", "exit", "local_access"]
    origin_id: str | None = None
    destination_id: str | None = None
    origin_label: str
    destination_label: str
    preferred_mode: str
    status: Literal["verified", "unsupported_by_provider", "unavailable", "missing"]
    route_evidence_ids: list[str] = Field(default_factory=list)

class WeatherContext(BaseModel):
    stop_id: str
    evidence_type: Literal["forecast", "seasonal", "unknown"]
    summary: str
    evidence_ids: list[str] = Field(default_factory=list)


class DestinationCoverage(BaseModel):
    """Deterministic evidence that one destination can support its trip share."""

    stop_id: str
    stop_name: str
    required_activity_minutes: int = Field(ge=0)
    researched_activity_minutes: int = Field(ge=0)
    primary_poi_count: int = Field(ge=0)
    backup_poi_count: int = Field(ge=0)
    distinct_poi_count: int = Field(ge=0, default=0)
    schedulable_poi_count: int = Field(ge=0, default=0)
    allocation_basis: str = "provisional_equal_share"
    sufficient: bool


class ResearchCoverage(BaseModel):
    """Duration-aware coverage summary used to gate planner handoff."""

    trip_days: int = Field(ge=1)
    expected_activity_minutes: int = Field(ge=0)
    researched_activity_minutes: int = Field(ge=0)
    destination_coverage: list[DestinationCoverage] = Field(default_factory=list)
    must_see_coverage: float = Field(ge=0, le=1, default=0.0)
    backup_poi_count: int = Field(ge=0, default=0)
    transport_coverage: float = Field(ge=0, le=1, default=1.0)
    required_transport_leg_count: int = Field(ge=0, default=0)
    represented_transport_leg_count: int = Field(ge=0, default=0)
    verified_transport_leg_count: int = Field(ge=0, default=0)
    sufficient_for_planning: bool = False


class StayBase(BaseModel):
    """A practical area recommendation, not live hotel inventory."""

    stop_id: str
    name: str
    coordinates: Coordinates
    rationale: str
    nearby_cluster_ids: list[str] = Field(default_factory=list)


class ResearchGap(BaseModel):
    """A deterministic, actionable missing fact or coverage requirement."""

    code: str
    severity: Literal["critical", "optional"]
    message: str
    stop_ids: list[str] = Field(default_factory=list)
    recommended_action: str


class ResearchQuality(BaseModel):
    completeness_score: float = Field(ge=0, le=1)
    critical_gaps: list[str] = Field(default_factory=list)
    optional_gaps: list[str] = Field(default_factory=list)
    sufficient_for_planning: bool = False


class ResearchRunMetrics(BaseModel):
    llm_calls: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_calls_by_model: dict[str, int] = Field(default_factory=dict)
    llm_input_tokens_by_model: dict[str, int] = Field(default_factory=dict)
    llm_output_tokens_by_model: dict[str, int] = Field(default_factory=dict)
    provider_requests: dict[str, int] = Field(default_factory=dict)
    provider_requests_by_priority: dict[str, dict[str, int]] = Field(
        default_factory=dict
    )
    cache_hits: int = 0
    cache_misses: int = 0
    estimated_cost_usd: float | None = None
    checkpoint_count: int = 0
    submission_count: int = 0
    duplicate_search_count: int = 0
    unsupported_tool_call_count: int = 0
    invalid_tool_argument_count: int = 0
    final_notebook_stop_count: int = 0
    final_notebook_poi_count: int = 0


class ResearchReport(BaseModel):
    status: ResearchStatus
    normalized_trip_context: ResearchInput
    destination_interpretation: str
    resolved_stops: list[ResolvedStop] = Field(default_factory=list)
    pois_by_stop: dict[str, list[POI]] = Field(default_factory=dict)
    geographic_clusters: list[GeographicCluster] = Field(default_factory=list)
    stay_bases: list[StayBase] = Field(default_factory=list)
    inter_stop_routes: list[RouteEvidence] = Field(default_factory=list)
    intra_stop_route_evidence: list[RouteEvidence] = Field(default_factory=list)
    transport_requirements: list[TransportRequirement] = Field(default_factory=list)
    weather_context: list[WeatherContext] = Field(default_factory=list)
    financial_context: dict[str, Any] = Field(default_factory=dict)
    current_facts: list[dict[str, Any]] = Field(default_factory=list)
    mandatory_constraints: list[str] = Field(default_factory=list)
    mandatory_requirements: list[MandatoryRequirement] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unresolved_gaps: list[str] = Field(default_factory=list)
    coverage: ResearchCoverage | None = None
    research_gaps: list[ResearchGap] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    quality: ResearchQuality
    metrics: ResearchRunMetrics
    architecture_version: str = "deterministic_research_v1"
    prompt_version: str | None = None

    @model_validator(mode="after")
    def complete_reports_need_planning_data(self) -> ResearchReport:
        has_pois = any(self.pois_by_stop.values())
        if self.status == "complete" and (
            not self.resolved_stops
            or not has_pois
            or self.quality.critical_gaps
            or not self.quality.sufficient_for_planning
        ):
            raise ValueError(
                "A complete report needs usable research, resolved stops, POIs, "
                "and no critical gaps."
            )
        if self.coverage is not None and (
            self.coverage.sufficient_for_planning
            != self.quality.sufficient_for_planning
        ):
            raise ValueError("Coverage handoff flag must match research quality.")
        evidence_ids = {item.id for item in self.evidence}
        stop_ids = {item.id for item in self.resolved_stops}
        poi_ids = {poi.id for pois in self.pois_by_stop.values() for poi in pois}

        def ensure_evidence(ids: list[str], subject: str) -> None:
            if not set(ids).issubset(evidence_ids):
                raise ValueError(
                    f"{subject} references evidence outside the evidence registry."
                )

        for stop in self.resolved_stops:
            ensure_evidence(stop.evidence_ids, f"stop {stop.id}")
        for stop_id, pois in self.pois_by_stop.items():
            if stop_id not in stop_ids:
                raise ValueError(f"POI bucket {stop_id} is not a resolved stop.")
            for poi in pois:
                if poi.stop_id != stop_id:
                    raise ValueError(f"POI {poi.id} has a mismatched stop_id.")
                ensure_evidence(poi.evidence_ids, f"POI {poi.id}")
        for cluster in self.geographic_clusters:
            if cluster.stop_id not in stop_ids or not set(cluster.poi_ids).issubset(
                poi_ids
            ):
                raise ValueError(
                    f"Cluster {cluster.id} has invalid stop or POI references."
                )
        for route in [*self.inter_stop_routes, *self.intra_stop_route_evidence]:
            ensure_evidence(
                route.evidence_ids, f"route {route.origin_id}->{route.destination_id}"
            )
        for requirement in self.transport_requirements:
            if requirement.status == "verified" and not requirement.route_evidence_ids:
                raise ValueError(
                    f"Verified transport requirement {requirement.id} has no evidence."
                )
            ensure_evidence(
                requirement.route_evidence_ids,
                f"transport requirement {requirement.id}",
            )
        for weather in self.weather_context:
            ensure_evidence(weather.evidence_ids, f"weather {weather.stop_id}")
        ensure_evidence(
            self.financial_context.get("source_ids", []), "financial context"
        )
        return self
