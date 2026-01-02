"""Stable, display-ready contracts produced by the deterministic compiler."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class PresentationFact(BaseModel):
    label: str
    value: str


class PresentationAlert(BaseModel):
    severity: str
    message: str
    day_number: int | None = None
    item_id: str | None = None


class PresentationBudget(BaseModel):
    currency_code: str
    estimated_minimum: Decimal
    estimated_maximum: Decimal
    total_budget: Decimal
    within_budget: bool
    summary: str
    categories: dict[str, str] = Field(default_factory=dict)


class PresentationTimelineItem(BaseModel):
    id: str
    kind: str
    icon: str
    start_time: str
    end_time: str
    duration_minutes: int
    title: str
    subtitle: str | None = None
    description: str | None = None
    estimated_cost: str | None = None
    map_url: str | None = None
    map_label: str | None = None
    dietary_tags: list[str] = Field(default_factory=list)


class PresentationDay(BaseModel):
    day_number: int
    date_label: str
    base_stop: str
    theme: str
    daily_cost: str
    alerts: list[PresentationAlert] = Field(default_factory=list)
    timeline: list[PresentationTimelineItem] = Field(default_factory=list)


class PresentationStay(BaseModel):
    stop: str
    nights: int
    description: str
    estimated_cost: str


class TripPresentation(BaseModel):
    """A frontend-neutral view model and its Markdown representation."""

    title: str
    summary: str
    quick_facts: list[PresentationFact]
    validation_status: str
    alerts: list[PresentationAlert]
    budget: PresentationBudget
    days: list[PresentationDay]
    stays: list[PresentationStay]
    checklist: list[str]
    markdown: str
