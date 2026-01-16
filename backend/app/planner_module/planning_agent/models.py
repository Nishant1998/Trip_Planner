"""Compact semantic planner state; deterministic tools own factual timelines."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..models.planning import DayKind


class SemanticDayDraft(BaseModel):
    day_number: int = Field(ge=1)
    base_stop_id: str | None = None
    day_kind: DayKind = DayKind.SIGHTSEEING
    theme: str = "Exploration"
    selected_poi_ids: list[str] = Field(default_factory=list)
    preferred_order: list[str] = Field(default_factory=list)
    confirmation_required_poi_ids: list[str] = Field(default_factory=list)
    start_anchor_id: str | None = None
    end_anchor_id: str | None = None
    notes: list[str] = Field(default_factory=list)


class PlanningNotebook(BaseModel):
    plan_version: int = 0
    base_sequence: list[str] = Field(default_factory=list)
    nights_by_base: dict[str, int] = Field(default_factory=dict)
    # Research stay areas are areas, not hotel inventory. Values are supplied
    # stay-area names/IDs or the explicit literal "unknown".
    stay_area_by_night: dict[int, str] = Field(default_factory=dict)
    days: list[SemanticDayDraft] = Field(default_factory=list)
    alternatives: dict[str, list[str]] = Field(default_factory=dict)
    latest_day_simulation_references: dict[int, str] = Field(default_factory=dict)
    latest_route_references: list[str] = Field(default_factory=list)
    latest_budget_reference: str | None = None
    latest_validation_reference: str | None = None
    unresolved_items: list[str] = Field(default_factory=list)
    guidance_items: list[str] = Field(default_factory=list)


class PlanningCheckpoint(BaseModel):
    base_sequence: list[str] = Field(default_factory=list)
    nights_by_base: dict[str, int] = Field(default_factory=dict)
    stay_area_by_night: dict[int, str] = Field(default_factory=dict)
    days: list[SemanticDayDraft] = Field(default_factory=list)
    alternatives: dict[str, list[str]] = Field(default_factory=dict)
    unresolved_items: list[str] = Field(default_factory=list)
    guidance_items: list[str] = Field(default_factory=list)


class PlanProposal(PlanningCheckpoint):
    """Bounded semantic suggestion; deterministic code preserves invariants."""

    rationale: list[str] = Field(default_factory=list)


class PlanningSubmission(BaseModel):
    summary: str = Field(min_length=1, max_length=1000)
    unresolved_items: list[str] = Field(default_factory=list)
    guidance_items: list[str] = Field(default_factory=list)


class HardConstraints(BaseModel):
    max_driving_minutes_per_day: int | None = None
    hard_day_end: str | None = None
    required_accessibility: list[str] = Field(default_factory=list)
    forbidden_transport_modes: list[str] = Field(default_factory=list)
    required_transport_modes: list[str] = Field(default_factory=list)
    raw_required_notes: list[str] = Field(default_factory=list)


class PlanningObservation(BaseModel):
    id: str
    tool: str
    status: str
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
