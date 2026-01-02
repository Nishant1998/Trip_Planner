"""Structured visit time and operational-buffer result."""

from typing import Literal

from pydantic import BaseModel, Field

from app.models.evidence import FactStatus


class VisitDuration(BaseModel):
    minimum_minutes: int = Field(ge=15)
    recommended_minutes: int = Field(ge=15)
    maximum_minutes: int = Field(ge=15)
    scheduled_minutes: int = Field(ge=15)
    activity_buffer_minutes: int = Field(ge=0)
    access_minutes: int = Field(default=0, ge=0)
    waiting_minutes: int = Field(default=0, ge=0)
    rest_minutes: int = Field(default=0, ge=0)
    exit_minutes: int = Field(default=0, ge=0)
    physical_effort: Literal["low", "moderate", "high"] | None = None
    last_entry_minutes_before_close: int | None = Field(default=None, ge=0)
    confidence: Literal["low", "medium", "high"] = "low"
    status: FactStatus
    basis: str
    source_ids: list[str] = Field(default_factory=list)
