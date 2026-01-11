"""Data carried between all research and planning nodes."""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages

from .config import Settings
from .models.planning import (
    PlannerInput,
    PlanningResult,
    PlanningWorld,
    TripRoutePlan,
    TripStrategy,
    ValidationIssue,
)
from .models.research import (
    POI,
    GeographicCluster,
    ResearchCoverage,
    ResearchGap,
    ResearchInput,
    ResearchQuality,
    ResearchReport,
    ResolvedStop,
    RouteEvidence,
    TransportRequirement,
    WeatherContext,
)


class PipelineState(TypedDict, total=False):
    # Research inputs and accumulated stage outputs.
    trip: ResearchInput
    stops: list[ResolvedStop]
    weather: list[WeatherContext]
    finance: dict[str, Any]
    candidates: dict[str, Any]
    shortlist: dict[str, Any]
    pois: dict[str, list[POI]]
    clusters: list[GeographicCluster]
    routes: tuple[list[RouteEvidence], list[RouteEvidence]]
    transport_requirements: list[TransportRequirement]
    quality: ResearchQuality
    coverage: ResearchCoverage
    research_gaps: list[ResearchGap]
    repair_pass: int
    repair_targets: list[str]
    repair_transport: bool
    repair_transport_targets: list[str]
    report: ResearchReport

    # V2 compact agent research state.  Old V1 fields stay temporarily so
    # existing saved-report and migration tests remain readable.
    messages: Annotated[list, add_messages]
    research_notebook: Any
    research_observations: list[dict[str, Any]]
    research_evidence: list[dict[str, Any]]
    candidate_report: ResearchReport | None
    validation_result: dict[str, Any] | None
    research_repair_count: int
    research_agent_steps: int

    # Planner V2 persistent-agent state. Exact timelines remain in the
    # existing typed planning fields below, not in the semantic notebook.
    planning_messages: Annotated[list, add_messages]
    planning_notebook: Any
    planning_observations: list[dict[str, Any]]
    planning_validation_result: dict[str, Any] | None
    planning_agent_steps: int
    planning_repair_count: int
    planning_candidate_result: Any

    # The handoff produces the same PlannerInput as the original file adapter.
    input: PlannerInput
    settings: Settings
    world: PlanningWorld
    strategy: TripStrategy
    result: PlanningResult
    before_issues: list[ValidationIssue]
    final_issues: list[ValidationIssue]
    final_validation: dict[str, Any]
    feasibility_actions: list[dict]
    reviewer_edits: list
    reviewer_decisions: list
    reviewer_findings: list
    metrics: dict
    route_cache: dict
    route_trace: list
    route_plan: TripRoutePlan
    mandatory_allocation: dict
    candidate_scores: dict
    utilization: list
