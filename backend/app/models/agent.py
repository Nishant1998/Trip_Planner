"""Shared, typed contracts for the three bounded planner agents."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from app.models.context import ResolvedDestination
from app.models.evidence import EvidenceBundle
from pydantic import BaseModel, Field


class ToolCallAudit(BaseModel):
    agent: Literal["research", "planner", "reviewer", "supervisor", "destination_research",
                   "itinerary_planning", "accommodation", "transport_booking", "response_aggregator"]
    call_id: str
    tool_name: str
    arguments: dict[str, object]
    status: Literal["ok", "invalid_arguments", "failed", "duplicate"]
    started_at: datetime
    duration_ms: int = Field(ge=0)
    result_summary: str


class AgentUsage(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    tool_calls: list[ToolCallAudit] = Field(default_factory=list)


class ResearchCompletion(BaseModel):
    status: Literal["complete", "needs_more_evidence", "blocked"]
    selected_place_ids: list[str] = Field(default_factory=list)
    unresolved_gaps: list[str] = Field(default_factory=list)
    summary: str


class ResearchAgentOutput(BaseModel):
    destination: ResolvedDestination
    evidence: EvidenceBundle
    completion: ResearchCompletion
    usage: AgentUsage


class PlannerCompletion(BaseModel):
    status: Literal["ready", "needs_repair", "blocked"]
    accepted_evaluation_id: str | None = None
    unresolved_issues: list[str] = Field(default_factory=list)
    summary: str


class ReviewStatus(StrEnum):
    PASS = "pass"
    RESEARCH_REQUIRED = "research_required"
    PLANNER_REPAIR_REQUIRED = "planner_repair_required"
    BLOCKED = "blocked"


class ReviewIssue(BaseModel):
    code: str
    severity: Literal["warning", "blocking"]
    affected_ids: list[str] = Field(default_factory=list)
    reason: str


class ReviewResult(BaseModel):
    status: ReviewStatus
    issues: list[ReviewIssue] = Field(default_factory=list)
    summary: str
