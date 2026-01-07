"""Public result returned to the caller for API response or caller-owned persistence."""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field
from .research import ResearchReport
from .planning import PlanningResult, TripRoutePlan, ValidationIssue


class PipelineResult(BaseModel):
    run_id: str
    research_report: ResearchReport
    planning_result: PlanningResult
    final_validation: dict[str, Any]
    validation_issues: list[ValidationIssue]
    route_plan: TripRoutePlan
    display_trip: dict[str, Any]
    metrics: dict[str, Any]
    timing_seconds: dict[str, float] = Field(default_factory=dict)
