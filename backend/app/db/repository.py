"""Abstract repository contract used by the runtime and orchestrator."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.models.api import JsonObject
from app.db.tables import (
    AgentStateRow,
    ArtifactRow,
    ConfirmedTripSnapshotRow,
    MessageRow,
    PendingCheckpointRow,
    PlanningArtifactRow,
    TripRow,
    TripPlanRow,
    UserRow,
)
from app.models.user import UserProfile
from app.models.usage import (
    LLMUsageDelta,
    LLMUsageSummary,
    ToolUsageDelta,
    ToolUsageSummary,
    TripUsageSummary,
)


class TripRepository(ABC):
    """Storage operations required by the collection runtime."""

    @abstractmethod
    def init_db(self) -> None:
        """Create or migrate the backing database schema."""

        ...

    @abstractmethod
    def ensure_test_user(self, user_id: int) -> None:
        """Ensure the local development user exists."""

        ...

    @abstractmethod
    def get_user(self, user_id: int) -> UserRow | None:
        """Return one user row by primary key."""

        ...

    @abstractmethod
    def get_user_by_email(self, email: str) -> UserRow | None:
        """Return one user row for an email address."""

        ...

    @abstractmethod
    def get_user_by_auth_provider(
        self, provider: str, provider_id: str
    ) -> UserRow | None:
        """Return one user row for an external auth identity."""

        ...

    @abstractmethod
    def create_user(
        self,
        email: str,
        picture: str | None,
        auth_provider: str,
        auth_provider_id: str,
        username: str | None = None,
    ) -> int:
        """Create a user and return the new id."""

        ...

    @abstractmethod
    def get_user_profile_obj(self, user_id: int) -> UserProfile | None:
        """Return the stored profile text bundle for a user."""

        ...

    @abstractmethod
    def update_user_profile_full(self, user_id: int, full_text: str) -> None:
        """Persist the full profile text for a user."""

        ...

    @abstractmethod
    def update_user_profile_compressed(
        self, user_id: int, compressed_text: str
    ) -> None:
        """Persist the compressed profile text for a user."""

        ...

    def record_llm_usage(
        self,
        *,
        user_id: int,
        trip_id: int,
        phase: str,
        records: list[LLMUsageDelta],
    ) -> None:
        """Persist batched per-agent token usage for one trip operation."""

        del user_id, trip_id, phase, records

    def get_trip_llm_usage(self, trip_id: int) -> LLMUsageSummary:
        """Return aggregate token usage for one trip."""

        del trip_id
        return LLMUsageSummary()

    def get_user_llm_usage(self, user_id: int) -> LLMUsageSummary:
        """Return aggregate token usage across all trips for one user."""

        del user_id
        return LLMUsageSummary()

    def record_tool_usage(
        self, *, user_id: int, trip_id: int, phase: str, records: list[ToolUsageDelta]
    ) -> None:
        """Persist measured external-tool use. Optional for lightweight test repos."""

        del user_id, trip_id, phase, records

    def get_trip_tool_usage(self, trip_id: int) -> ToolUsageSummary:
        del trip_id
        return ToolUsageSummary()

    def get_trip_usage(self, trip_id: int) -> TripUsageSummary:
        return TripUsageSummary(
            llm=self.get_trip_llm_usage(trip_id),
            tools=self.get_trip_tool_usage(trip_id),
        )

    @abstractmethod
    def get_latest_active_trip(self, user_id: int) -> TripRow | None:
        """Return the latest non-archived trip for one user."""

        ...

    @abstractmethod
    def create_trip(self, owner_id: int) -> TripRow:
        """Create a new trip row for one owner."""

        ...

    @abstractmethod
    def get_trip(self, trip_id: int) -> TripRow | None:
        """Return one trip row by primary key."""

        ...

    @abstractmethod
    def get_trip_by_slug(self, slug: str) -> TripRow | None:
        """Return one trip row by slug."""

        ...

    @abstractmethod
    def list_trips_by_user(self, user_id: int) -> list[TripRow]:
        """List trips for one user, newest first."""

        ...

    @abstractmethod
    def update_trip_status(self, trip_id: int, status: str) -> None:
        """Update the stored status for a trip."""

        ...

    @abstractmethod
    def save_trip_state(self, trip_id: int, agent_state_json: str) -> None:
        """Persist the full serialized trip memory for one trip."""

        ...

    @abstractmethod
    def sync_trip_scalars(self, trip_id: int, **fields) -> None:
        """Update denormalized trip columns from the current memory."""

        ...

    @abstractmethod
    def get_agent_state(self, trip_id: int) -> AgentStateRow | None:
        """Return the expanded agent-state payload for a trip."""

        ...

    @abstractmethod
    def insert_message(
        self,
        trip_id: int,
        role: str,
        content: str,
        sender_id: int | None = None,
        meta: JsonObject | None = None,
    ) -> int:
        """Insert one raw chat message row and return its id."""

        ...

    @abstractmethod
    def get_recent_messages(self, trip_id: int, limit: int = 20) -> list[MessageRow]:
        """Return the latest messages for one trip."""

        ...

    @abstractmethod
    def get_all_messages(self, trip_id: int) -> list[MessageRow]:
        """Return the full ordered message history for one trip."""

        ...

    @abstractmethod
    def list_messages(self, trip_id: int) -> list[MessageRow]:
        """List messages for one trip in ascending order."""

        ...

    @abstractmethod
    def add_message(
        self,
        trip_id: int,
        role: str,
        content: str,
        sender_id: int | None = None,
        meta: JsonObject | None = None,
    ) -> MessageRow:
        """Insert and return one normalized message payload."""

        ...

    @abstractmethod
    def save_artifact(
        self, trip_id: int, artifact_type: str, data: JsonObject, version: int = 1
    ) -> int:
        """Persist one planning artifact version and return its id."""

        ...

    @abstractmethod
    def get_artifact(self, trip_id: int, artifact_type: str) -> ArtifactRow | None:
        """Return the latest non-stale artifact of one type."""

        ...

    @abstractmethod
    def mark_artifacts_stale(self, trip_id: int, artifact_types: list[str]) -> None:
        """Mark the requested artifact types as stale for one trip."""

        ...

    @abstractmethod
    def get_latest_artifact(self, trip_id: int) -> ArtifactRow | None:
        """Return the newest artifact row for one trip."""

        ...

    @abstractmethod
    def save_checkpoint(
        self,
        trip_id: int,
        checkpoint_type: str,
        payload_json: str,
        is_pending: bool = True,
    ) -> None:
        """Save one checkpoint artifact for the current trip."""

        ...

    @abstractmethod
    def get_pending_checkpoint(self, trip_id: int) -> PendingCheckpointRow | None:
        """Return the currently pending checkpoint, if any."""

        ...

    @abstractmethod
    def clear_pending_checkpoints(self, trip_id: int) -> None:
        """Mark the latest checkpoint as no longer pending."""

        ...

    @abstractmethod
    def save_planning_artifact(
        self,
        trip_id: int,
        artifact_type: str,
        confidence: str,
        payload_json: str,
    ) -> None:
        """Persist a named planning artifact payload."""

        ...

    @abstractmethod
    def get_latest_planning_artifact(
        self, trip_id: int, artifact_type: str
    ) -> PlanningArtifactRow | None:
        """Return the latest planning artifact payload for one type."""

        ...

    def save_current_research_report(
        self,
        trip_id: int,
        snapshot_id: int,
        report: JsonObject,
        metadata: JsonObject,
    ) -> int:
        """Save one current, reusable research handoff for a confirmed snapshot."""

        del trip_id, snapshot_id, report, metadata
        raise NotImplementedError

    def get_current_research_report(
        self, trip_id: int, snapshot_id: int
    ) -> ArtifactRow | None:
        """Return the current usable research handoff for this exact snapshot."""

        del trip_id, snapshot_id
        return None

    @abstractmethod
    def create_confirmed_trip_snapshot(
        self,
        trip_id: int,
        payload: JsonObject,
        fingerprint: str,
    ) -> ConfirmedTripSnapshotRow:
        """Save or return the current immutable Phase 1 planning input."""

        ...

    @abstractmethod
    def get_current_confirmed_trip_snapshot(
        self,
        trip_id: int,
    ) -> ConfirmedTripSnapshotRow | None:
        """Return the snapshot that remains valid for the current trip version."""

        ...

    def get_message_by_client_message_id(
        self, trip_id: int, client_message_id: str
    ) -> MessageRow | None:
        """Return the already accepted user message for a durable client retry."""
        return None

    @abstractmethod
    def create_user_session(
        self, user_id: int, token_hash: str, expires_at_iso: str
    ) -> None:
        """Store an active login session token."""
        ...

    @abstractmethod
    def get_user_by_session_token(self, token_hash: str) -> UserRow | None:
        """Return the user associated with a valid active session token."""
        ...

    @abstractmethod
    def delete_user_session(self, token_hash: str) -> None:
        """Revoke one active session token."""
        ...

    @abstractmethod
    def update_user_profile(self, user_id: int, **fields: Any) -> UserRow | None:
        """Update editable profile fields for a user."""
        ...

    @abstractmethod
    def delete_trip(self, trip_id: int, owner_id: int) -> bool:
        """Delete one trip owned by a user."""
        ...

    @abstractmethod
    def save_trip_plan(
        self,
        trip_id: int,
        job_id: str | None,
        delivery_status: str,
        plan_json: JsonObject,
        markdown_summary: str,
    ) -> TripPlanRow:
        """Save a generated day-by-day travel plan."""
        ...

    @abstractmethod
    def get_active_trip_plan(self, trip_id: int) -> TripPlanRow | None:
        """Return the current active travel plan for a trip."""
        ...
