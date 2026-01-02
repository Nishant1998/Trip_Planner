"""Typed storage-boundary models for pure Neon PostgreSQL database."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.api import JsonObject


class RepositoryRecord(BaseModel):
    """Base record model with dictionary access compatibility."""

    model_config = ConfigDict(extra="ignore")

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class UserRow(RepositoryRecord):
    """Stored user row."""

    id: int
    email: str
    username: str | None = None
    name: str | None = None
    picture: str | None = None
    role: str = "user"
    phone_number: str | None = None
    is_phone_verified: bool = False
    dob: str | date | None = None
    gender: str | None = None
    citizenship: str | None = None
    home_city: str | None = None
    spending_tier: str = "standard"
    preferences: JsonObject = Field(default_factory=dict)
    auth_provider: str = "google"
    auth_provider_id: str | None = None
    profile_full: str | None = None
    profile_compressed: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class UserSessionRow(RepositoryRecord):
    """Stored user authentication session."""

    id: int
    user_id: int
    session_token_hash: str
    expires_at: str
    created_at: str | None = None


class TripRow(RepositoryRecord):
    """Stored trip metadata."""

    id: int
    owner_id: int
    slug: str | None = None
    status: str = "draft"
    trip_name: str | None = None
    trip_description: str | None = None
    cover_image_url: str | None = None
    origin_city: str | None = None
    destination_type: str | None = None
    destination_items: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    number_of_days: int | None = None
    travelers_adults: int = 1
    travelers_children: int = 0
    travelers_seniors: int = 0
    budget_currency: str | None = "INR"
    budget_amount: int | None = None
    trip_scope: str | None = None
    spending_tier: str | None = None
    travel_style: str | None = None
    trip_purpose: str | None = None
    factors: list[str] = Field(default_factory=list)
    agent_state: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class MessageRow(RepositoryRecord):
    """Stored trip conversation message."""

    id: int
    trip_id: int
    role: str
    content: str
    meta: JsonObject = Field(default_factory=dict)
    created_at: str | None = None


class ConfirmedTripSnapshotRow(RepositoryRecord):
    """Immutable confirmed trip memory snapshot."""

    id: int
    trip_id: int
    version: int
    fingerprint: str
    payload: JsonObject
    is_current: bool = True
    created_at: str | None = None


class PlanningArtifactRow(RepositoryRecord):
    """Cached intermediate planning intelligence artifact."""

    id: int | None = None
    trip_id: int | None = None
    artifact_type: str
    # Public artifact reads expose this as ``payload``.  The storage-specific
    # alias below additionally exposes the same JSON as ``data``.
    payload: JsonObject = Field(default_factory=dict)
    is_stale: bool = False
    # ``is_stale`` remains for existing artifact consumers.  New reusable
    # research records use this positive selection flag instead.
    is_current: bool = True
    version: int = 1
    created_at: str | None = None


class TripPlanRow(RepositoryRecord):
    """Finalized verified day-by-day itinerary."""

    id: str
    trip_id: int
    job_id: str | None = None
    version: int = 1
    delivery_status: str = "ready"
    is_active: bool = True
    plan_json: JsonObject = Field(default_factory=dict)
    markdown_summary: str = ""
    created_at: str | None = None


class LlmUsageRow(RepositoryRecord):
    """Per-operation LLM compute cost audit."""

    id: int
    trip_id: int | None = None
    user_id: int | None = None
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    operation_type: str = "turn"
    created_at: str | None = None


class ToolUsageRow(RepositoryRecord):
    """External tool execution audit."""

    id: int
    trip_id: int | None = None
    tool_name: str
    execution_time_ms: int = 0
    success: bool = True
    error_message: str | None = None
    created_at: str | None = None


class ArtifactRow(PlanningArtifactRow):
    """Storage-shaped artifact row used by the PostgreSQL repository."""

    data: JsonObject = Field(default_factory=dict)


class PendingCheckpointRow(RepositoryRecord):
    """Pending collection checkpoint record."""

    trip_id: int
    checkpoint_id: str = ""
    payload: JsonObject = Field(default_factory=dict)


class AgentStateRow(RepositoryRecord):
    """Agent runtime state wrapper."""

    trip_id: int | None = None
    status: str = "draft"
    trip_status: str | None = None
    memory_json: str | None = None
    memory: Any = None
    updated_at: str | None = None
