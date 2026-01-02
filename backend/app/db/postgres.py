"""PostgreSQL repository implementation for Neon/online deployments."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.domain.cover_images import resolve_destination_cover_image

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Json
from pydantic import ValidationError

from app.exceptions import RepositoryError
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
from app.models.enums import PlanningArtifactType, TripStatus
from app.models.usage import (
    LLMUsageDelta,
    LLMUsageSummary,
    ToolUsageDelta,
    ToolUsageSummary,
)
from app.models.state import (
    build_agent_memory_view,
    initialize_trip_state,
    load_runtime_memory,
    serialize_runtime_memory,
)
from app.models.user import UserProfile
from app.db.repository import TripRepository


def utc_now() -> str:
    """Return the current UTC timestamp in the app's persisted string format."""

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class PostgresTripRepository(TripRepository):
    """PostgreSQL-backed repository used when ``DATABASE_URL`` is configured."""

    def __init__(self, database_url: str, *, schema: str | None = "public") -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for PostgresTripRepository")
        self.database_url = database_url
        self.schema = schema or "public"
        self._write_lock = threading.Lock()

    def init_db(self) -> None:
        """Create the PostgreSQL schema when it does not already exist."""

        with self._connect() as conn:
            conn.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    sql.Identifier(self.schema)
                )
            )
            for migration_path in sorted(
                (Path(__file__).resolve().parent / "migrations").glob("*.sql")
            ):
                for statement in migration_path.read_text(encoding="utf-8").split(";"):
                    normalized = statement.strip()
                    if normalized:
                        conn.execute(normalized)
            conn.commit()

    def check_connection(self) -> None:
        """Check the initial tables without creating or migrating anything."""
        with self._connect() as conn:
            for table in ("users", "trips"):
                row = conn.execute(
                    "SELECT to_regclass(%s) AS relation",
                    (f"{self.schema}.{table}",),
                ).fetchone()
                if row["relation"] is None:
                    raise RuntimeError(
                        f"Missing {table} table. Run uv run python -m app.db init."
                    )

    def ensure_test_user(self, user_id: int) -> None:
        """Create the default local development user when missing."""

        now = utc_now()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (
                    id, username, email, picture, auth_provider, auth_provider_id,
                    profile_full, profile_compressed, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    user_id,
                    "dev-user",
                    "dev@riya.local",
                    None,
                    "google",
                    f"dev-google-{user_id}",
                    "Home city: Mumbai. Prefers thoughtful trip planning and concise suggestions.",
                    "The user is based in Mumbai and prefers concise travel planning help.",
                    now,
                    now,
                ),
            )
            self._refresh_sequence(conn, "users", "id")
            conn.commit()

    def get_user(self, user_id: int) -> UserRow | None:
        """Return one user row by id."""

        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = %s LIMIT 1", (user_id,)
            ).fetchone()
        return self._user_row_to_model(row)

    def get_user_by_email(self, email: str) -> UserRow | None:
        """Return one user row by email address."""

        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE email = %s LIMIT 1", (email,)
            ).fetchone()
        return self._user_row_to_model(row)

    def get_user_by_auth_provider(
        self,
        provider: str,
        provider_id: str,
    ) -> UserRow | None:
        """Return one user row by external auth provider identity."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM users
                WHERE auth_provider = %s AND auth_provider_id = %s
                LIMIT 1
                """,
                (provider, provider_id),
            ).fetchone()
        return self._user_row_to_model(row)

    def create_user(
        self,
        email: str,
        picture: str | None,
        auth_provider: str,
        auth_provider_id: str,
        username: str | None = None,
    ) -> int:
        """Insert a user row and return the new id."""

        now = utc_now()
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO users (
                    username, email, picture, auth_provider, auth_provider_id,
                    profile_full, profile_compressed, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, '', '', %s, %s)
                RETURNING id
                """,
                (username, email, picture, auth_provider, auth_provider_id, now, now),
            ).fetchone()
            conn.commit()
        return int(row["id"])

    def get_user_profile_obj(self, user_id: int) -> UserProfile | None:
        """Return the stored full and compressed profile text for a user."""

        with self._connect() as conn:
            row = conn.execute(
                "SELECT profile_full, profile_compressed FROM users WHERE id = %s LIMIT 1",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return UserProfile(
            full_text=row["profile_full"] or "",
            compressed_text=row["profile_compressed"] or "",
            llm_usage=self.get_user_llm_usage(user_id),
        )

    def update_user_profile_full(self, user_id: int, full_text: str) -> None:
        """Update the full profile text for one user."""

        now = utc_now()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET profile_full = %s, updated_at = %s WHERE id = %s",
                (full_text, now, user_id),
            )
            conn.commit()

    def update_user_profile_compressed(
        self, user_id: int, compressed_text: str
    ) -> None:
        """Update the compressed profile text for one user."""

        now = utc_now()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET profile_compressed = %s, updated_at = %s WHERE id = %s",
                (compressed_text, now, user_id),
            )
            conn.commit()

    def record_llm_usage(
        self,
        *,
        user_id: int,
        trip_id: int,
        phase: str,
        records: list[LLMUsageDelta],
    ) -> None:
        """Atomically add one operation's token counts to stored aggregates."""

        if not records:
            return

        now = utc_now()
        with self._write_lock, self._connect() as conn:
            for record in records:
                conn.execute(
                    """
                    INSERT INTO llm_usage (
                        user_id, trip_id, phase, agent_name, model,
                        requests, input_tokens, output_tokens,
                        cached_input_tokens, cache_write_tokens, reasoning_tokens,
                        created_at, updated_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s,
                        %s, %s
                    )
                    ON CONFLICT (user_id, trip_id, phase, agent_name, model)
                    DO UPDATE SET
                        requests = llm_usage.requests + EXCLUDED.requests,
                        input_tokens = llm_usage.input_tokens + EXCLUDED.input_tokens,
                        output_tokens = llm_usage.output_tokens + EXCLUDED.output_tokens,
                        cached_input_tokens = (
                            llm_usage.cached_input_tokens + EXCLUDED.cached_input_tokens
                        ),
                        cache_write_tokens = (
                            llm_usage.cache_write_tokens + EXCLUDED.cache_write_tokens
                        ),
                        reasoning_tokens = (
                            llm_usage.reasoning_tokens + EXCLUDED.reasoning_tokens
                        ),
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        user_id,
                        trip_id,
                        phase,
                        record.agent_name,
                        record.model,
                        record.requests,
                        record.input_tokens,
                        record.output_tokens,
                        record.cached_input_tokens,
                        record.cache_write_tokens,
                        record.reasoning_tokens,
                        now,
                        now,
                    ),
                )
            conn.commit()

    def get_trip_llm_usage(self, trip_id: int) -> LLMUsageSummary:
        """Return all recorded LLM token usage for one trip."""

        return self._get_llm_usage_summary("trip_id", trip_id)

    def get_user_llm_usage(self, user_id: int) -> LLMUsageSummary:
        """Return all recorded LLM token usage for one user."""

        return self._get_llm_usage_summary("user_id", user_id)

    def record_tool_usage(
        self, *, user_id: int, trip_id: int, phase: str, records: list[ToolUsageDelta]
    ) -> None:
        if not records:
            return
        now = utc_now()
        with self._write_lock, self._connect() as conn:
            for record in records:
                conn.execute(
                    """INSERT INTO tool_usage (user_id, trip_id, phase, agent_name, provider, tool_name, calls, failed_calls, duration_ms, created_at, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (user_id, trip_id, phase, agent_name, provider, tool_name) DO UPDATE SET
                    calls = tool_usage.calls + EXCLUDED.calls,
                    failed_calls = tool_usage.failed_calls + EXCLUDED.failed_calls,
                    duration_ms = tool_usage.duration_ms + EXCLUDED.duration_ms,
                    updated_at = EXCLUDED.updated_at""",
                    (
                        user_id,
                        trip_id,
                        phase,
                        record.agent_name,
                        record.provider,
                        record.tool_name,
                        record.calls,
                        record.failed_calls,
                        record.duration_ms,
                        now,
                        now,
                    ),
                )
            conn.commit()

    def get_trip_tool_usage(self, trip_id: int) -> ToolUsageSummary:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(calls), 0) AS calls, COALESCE(SUM(failed_calls), 0) AS failed_calls, COALESCE(SUM(duration_ms), 0) AS duration_ms FROM tool_usage WHERE trip_id = %s",
                (trip_id,),
            ).fetchone()
        return ToolUsageSummary.model_validate(dict(row or {}))

    def _get_llm_usage_summary(self, column: str, value: int) -> LLMUsageSummary:
        if column not in {"trip_id", "user_id"}:
            raise ValueError("Unsupported LLM usage scope")

        query = sql.SQL(
            """
            SELECT
                COALESCE(SUM(requests), 0) AS requests,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
                COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens
            FROM llm_usage
            WHERE {} = %s
            """
        ).format(sql.Identifier(column))
        with self._connect() as conn:
            row = conn.execute(query, (value,)).fetchone()
        return LLMUsageSummary.model_validate(dict(row or {}))

    def get_latest_active_trip(self, user_id: int) -> TripRow | None:
        """Return the latest trip that is still active for one user."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM trips
                WHERE owner_id = %s
                  AND status NOT IN ('archived', 'cancelled')
                ORDER BY id DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        return self._trip_row_to_model(row)

    def create_trip(self, owner_id: int) -> TripRow:
        """Create a new trip row and return the stored record."""

        now = utc_now()
        temporary_slug = f"pending-{uuid.uuid4().hex}"
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO trips (
                    owner_id, slug, trip_name, trip_description,
                    status, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    owner_id,
                    temporary_slug,
                    "Untitled trip",
                    "",
                    TripStatus.DRAFT.value,
                    now,
                    now,
                ),
            ).fetchone()
            trip_id = int(row["id"])
            slug = secrets.token_hex(6)
            default_cover = resolve_destination_cover_image(None)
            initial_state_json = json.dumps(
                initialize_trip_state(slug=slug),
                ensure_ascii=False,
            )
            conn.execute(
                "UPDATE trips SET slug = %s, cover_image_url = %s, agent_state = %s, updated_at = %s WHERE id = %s",
                (slug, default_cover, initial_state_json, now, trip_id),
            )
            self._sync_trip_scalars_in_tx(
                conn,
                trip_id,
                self._trip_scalar_fields_from_memory_json(initial_state_json),
            )
            conn.commit()
        trip = self.get_trip(trip_id)
        if trip is None:
            raise RepositoryError("create_trip", RuntimeError("Trip row missing"))
        return trip

    def get_trip(self, trip_id: int) -> TripRow | None:
        """Return one trip row by id."""

        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM trips WHERE id = %s LIMIT 1", (trip_id,)
            ).fetchone()
        return self._trip_row_to_model(row)

    def get_trip_by_slug(self, slug: str) -> TripRow | None:
        """Return one trip row by slug."""

        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM trips WHERE slug = %s LIMIT 1", (slug,)
            ).fetchone()
        return self._trip_row_to_model(row)

    def list_trips_by_user(self, user_id: int) -> list[TripRow]:
        """List trips for one user, newest first."""

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trips WHERE owner_id = %s ORDER BY updated_at DESC, id DESC",
                (user_id,),
            ).fetchall()
        return [self._trip_row_to_model(row) for row in rows if row is not None]

    def update_trip_status(self, trip_id: int, status: str) -> None:
        """Persist a new status value for a trip."""

        now = utc_now()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE trips SET status = %s, updated_at = %s WHERE id = %s",
                (status, now, trip_id),
            )
            conn.commit()

    def save_trip_state(self, trip_id: int, agent_state_json: str) -> None:
        """Persist the serialized trip memory and sync denormalized columns."""

        now = utc_now()
        normalized_json = serialize_runtime_memory(
            load_runtime_memory(agent_state_json)
        )
        scalar_fields = self._trip_scalar_fields_from_memory_json(normalized_json)
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE trips SET agent_state = %s, updated_at = %s WHERE id = %s",
                (normalized_json, now, trip_id),
            )
            self._sync_trip_scalars_in_tx(conn, trip_id, scalar_fields)
            conn.commit()

    def sync_trip_scalars(self, trip_id: int, **fields) -> None:
        """Update selected denormalized trip columns."""

        with self._write_lock, self._connect() as conn:
            self._sync_trip_scalars_in_tx(conn, trip_id, fields)
            conn.commit()

    def get_agent_state(self, trip_id: int) -> AgentStateRow | None:
        """Return the stored memory plus a few convenience scalar fields."""

        trip = self.get_trip(trip_id)
        if trip is None:
            return None
        raw = trip.get("agent_state")
        if not raw:
            return None
        memory = load_runtime_memory(raw)
        agent_view = build_agent_memory_view(memory, "collection")
        agent_view_json = json.dumps(agent_view, ensure_ascii=False)
        return AgentStateRow(
            status=memory.metadata.status.value,
            trip_status=memory.metadata.status.value,
            memory_json=agent_view_json,
            memory=agent_view,
            updated_at=trip.updated_at,
        )

    def insert_message(
        self,
        trip_id: int,
        role: str,
        content: str,
        sender_id: int | None = None,
        meta: JsonObject | None = None,
    ) -> int:
        """Insert one raw message row and return its id."""
        return self.add_message(
            trip_id,
            role,
            content,
            sender_id=sender_id,
            meta=meta,
        ).id

    def get_message_by_client_message_id(
        self, trip_id: int, client_message_id: str
    ) -> MessageRow | None:
        """Look up a previously accepted chat submission durably."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT id, trip_id, sender_id, role, content, meta, created_at
                   FROM messages WHERE trip_id = %s AND meta->>'client_message_id' = %s
                   ORDER BY id DESC LIMIT 1""",
                (trip_id, client_message_id),
            ).fetchone()
        return self._message_row_to_model(row) if row is not None else None

    def get_recent_messages(self, trip_id: int, limit: int = 20) -> list[MessageRow]:
        """Return the latest messages for a trip in chronological order."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, trip_id, sender_id, role, content, meta, created_at
                FROM messages
                WHERE trip_id = %s
                ORDER BY id DESC
                LIMIT %s
                """,
                (trip_id, limit),
            ).fetchall()
        items = [self._message_row_to_model(row) for row in rows if row is not None]
        items.reverse()
        return items

    def get_all_messages(self, trip_id: int) -> list[MessageRow]:
        """Return the complete message history for a trip."""

        return self.list_messages(trip_id)

    def list_messages(self, trip_id: int) -> list[MessageRow]:
        """Return all stored messages for a trip."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, trip_id, sender_id, role, content, meta, created_at
                FROM messages
                WHERE trip_id = %s
                ORDER BY id ASC
                """,
                (trip_id,),
            ).fetchall()
        return [self._message_row_to_model(row) for row in rows if row is not None]

    def add_message(
        self,
        trip_id: int,
        role: str,
        content: str,
        sender_id: int | None = None,
        meta: JsonObject | None = None,
    ) -> MessageRow:
        """Insert and return one normalized message payload."""
        now = utc_now()
        meta_payload = meta or {}
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO messages (trip_id, sender_id, role, content, meta, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, trip_id, sender_id, role, content, meta, created_at
                """,
                (trip_id, sender_id, role, content, Json(meta_payload), now),
            ).fetchone()
            conn.execute(
                "UPDATE trips SET updated_at = %s WHERE id = %s", (now, trip_id)
            )
            conn.commit()
        message = self._message_row_to_model(row)
        if message is None:
            raise RepositoryError("add_message", RuntimeError("Message row missing"))
        return message

    def save_artifact(
        self, trip_id: int, artifact_type: str, data: JsonObject, version: int = 1
    ) -> int:
        """Save one planning artifact version and return its row id."""

        now = utc_now()
        normalized_type = self._normalize_artifact_type(artifact_type)
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO planning_artifacts (trip_id, artifact_type, data, is_stale, version, created_at)
                VALUES (%s, %s, %s, FALSE, %s, %s)
                RETURNING id
                """,
                (trip_id, normalized_type, Json(data), version, now),
            ).fetchone()
            conn.execute(
                "UPDATE trips SET updated_at = %s WHERE id = %s", (now, trip_id)
            )
            conn.commit()
        return int(row["id"])

    def get_artifact(self, trip_id: int, artifact_type: str) -> ArtifactRow | None:
        """Return the latest non-stale artifact for one trip and type."""

        normalized_type = self._normalize_artifact_type(artifact_type)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM planning_artifacts
                WHERE trip_id = %s AND artifact_type = %s AND is_stale = FALSE
                ORDER BY version DESC, id DESC
                LIMIT 1
                """,
                (trip_id, normalized_type),
            ).fetchone()
        return self._artifact_row_to_model(row)

    def mark_artifacts_stale(self, trip_id: int, artifact_types: list[str]) -> None:
        """Mark the requested artifact types as stale for one trip."""

        normalized = [self._normalize_artifact_type(item) for item in artifact_types]
        if not normalized:
            return
        placeholders = ", ".join("%s" for _ in normalized)
        with self._write_lock, self._connect() as conn:
            conn.execute(
                f"""
                UPDATE planning_artifacts
                SET is_stale = TRUE
                WHERE trip_id = %s AND artifact_type IN ({placeholders})
                """,
                (trip_id, *normalized),
            )
            conn.commit()

    def get_latest_artifact(self, trip_id: int) -> ArtifactRow | None:
        """Return the newest artifact row for a trip."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM planning_artifacts
                WHERE trip_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (trip_id,),
            ).fetchone()
        return self._artifact_row_to_model(row)

    def save_checkpoint(
        self,
        trip_id: int,
        checkpoint_type: str,
        payload_json: str,
        is_pending: bool = True,
    ) -> None:
        """Persist one checkpoint payload as an artifact."""

        payload = json.loads(payload_json)
        payload["checkpoint_type"] = checkpoint_type
        payload["pending"] = bool(is_pending)
        now = utc_now()
        with self._write_lock, self._connect() as conn:
            version = self._next_artifact_version_in_tx(conn, trip_id)
            conn.execute(
                """
                UPDATE planning_artifacts
                SET is_stale = TRUE
                WHERE trip_id = %s AND artifact_type = %s
                """,
                (trip_id, PlanningArtifactType.CHECKPOINT.value),
            )
            conn.execute(
                """
                INSERT INTO planning_artifacts (trip_id, artifact_type, data, is_stale, version, created_at)
                VALUES (%s, %s, %s, FALSE, %s, %s)
                """,
                (
                    trip_id,
                    PlanningArtifactType.CHECKPOINT.value,
                    Json(payload),
                    version,
                    now,
                ),
            )
            conn.execute(
                "UPDATE trips SET updated_at = %s WHERE id = %s", (now, trip_id)
            )
            conn.commit()

    def get_pending_checkpoint(self, trip_id: int) -> PendingCheckpointRow | None:
        """Return the current pending checkpoint payload, if one exists."""

        artifact = self.get_artifact(trip_id, PlanningArtifactType.CHECKPOINT.value)
        if artifact is None:
            return None
        payload = artifact.data
        if not payload.get("pending", False):
            return None
        return PendingCheckpointRow(
            checkpoint_type=payload.get("checkpoint_type", "text_reply"),
            is_pending=True,
            payload=payload,
        )

    def clear_pending_checkpoints(self, trip_id: int) -> None:
        """Mark the latest checkpoint payload as no longer pending."""

        row = self._get_latest_artifact_row(
            trip_id, PlanningArtifactType.CHECKPOINT.value
        )
        if row is None:
            return
        payload = self._decode_json(row["data"])
        payload["pending"] = False
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE planning_artifacts SET data = %s, is_stale = TRUE WHERE id = %s",
                (Json(payload), row["id"]),
            )
            conn.commit()

    def save_planning_artifact(
        self,
        trip_id: int,
        artifact_type: str,
        confidence: str,
        payload_json: str,
    ) -> None:
        """Persist one named planning artifact payload."""

        payload = json.loads(payload_json)
        if isinstance(payload, dict) and confidence and "confidence" not in payload:
            payload["confidence"] = confidence
        normalized_type = self._normalize_artifact_type(artifact_type)
        now = utc_now()
        with self._write_lock, self._connect() as conn:
            version = self._next_artifact_version_in_tx(conn, trip_id)
            conn.execute(
                """
                INSERT INTO planning_artifacts (trip_id, artifact_type, data, is_stale, version, created_at)
                VALUES (%s, %s, %s, FALSE, %s, %s)
                """,
                (trip_id, normalized_type, Json(payload), version, now),
            )
            conn.execute(
                "UPDATE trips SET updated_at = %s WHERE id = %s", (now, trip_id)
            )
            conn.commit()

    def get_latest_planning_artifact(
        self, trip_id: int, artifact_type: str
    ) -> PlanningArtifactRow | None:
        """Return the latest planning artifact in frontend-friendly shape."""

        artifact = self.get_artifact(trip_id, artifact_type)
        if artifact is None:
            return None
        return PlanningArtifactRow(
            artifact_type=artifact.artifact_type,
            payload=artifact.data,
            created_at=artifact.created_at,
            version=artifact.version,
            is_stale=artifact.is_stale,
            is_current=artifact.is_current,
        )

    def save_current_research_report(
        self,
        trip_id: int,
        snapshot_id: int,
        report: JsonObject,
        metadata: JsonObject,
    ) -> int:
        """Atomically replace the reusable research result for one trip snapshot."""

        now = utc_now()
        payload = {"snapshot_id": snapshot_id, "report": report, "metadata": metadata}
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE planning_artifacts
                SET is_current = FALSE, is_stale = TRUE
                WHERE trip_id = %s AND artifact_type = 'research_report'
                """,
                (trip_id,),
            )
            version = self._next_artifact_version_in_tx(conn, trip_id)
            row = conn.execute(
                """
                INSERT INTO planning_artifacts
                    (trip_id, artifact_type, data, is_current, is_stale, version, created_at)
                VALUES (%s, 'research_report', %s, TRUE, FALSE, %s, %s)
                RETURNING id
                """,
                (trip_id, Json(payload), version, now),
            ).fetchone()
            conn.commit()
        return int(row["id"])

    def get_current_research_report(
        self, trip_id: int, snapshot_id: int
    ) -> ArtifactRow | None:
        """Load a usable saved report only when it belongs to this input snapshot."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM planning_artifacts
                WHERE trip_id = %s
                  AND artifact_type = 'research_report'
                  AND is_current = TRUE
                  AND data->>'snapshot_id' = %s
                ORDER BY version DESC, id DESC
                LIMIT 1
                """,
                (trip_id, str(snapshot_id)),
            ).fetchone()
        return self._artifact_row_to_model(row)

    def create_confirmed_trip_snapshot(
        self,
        trip_id: int,
        payload: JsonObject,
        fingerprint: str,
    ) -> ConfirmedTripSnapshotRow:
        """Store one immutable approved input, reusing an identical current one."""

        if not fingerprint or len(fingerprint) != 64:
            raise ValueError(
                "confirmed snapshot fingerprint must be a SHA-256 hex digest"
            )
        canonical_payload = json.loads(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        )
        expected = hashlib.sha256(
            json.dumps(
                canonical_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if fingerprint != expected:
            raise ValueError(
                "confirmed snapshot fingerprint does not match its payload"
            )

        now = utc_now()
        with self._write_lock, self._connect() as conn:
            existing = conn.execute(
                """
                SELECT * FROM confirmed_trip_snapshots
                WHERE trip_id = %s AND fingerprint = %s AND is_current = TRUE
                ORDER BY version DESC, id DESC
                LIMIT 1
                """,
                (trip_id, fingerprint),
            ).fetchone()
            if existing is not None:
                return self._confirmed_snapshot_row_to_model(existing)
            next_version_row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS next_version "
                "FROM confirmed_trip_snapshots WHERE trip_id = %s",
                (trip_id,),
            ).fetchone()
            conn.execute(
                """
                UPDATE planning_artifacts
                SET is_current = FALSE, is_stale = TRUE
                WHERE trip_id = %s AND artifact_type = 'research_report'
                """,
                (trip_id,),
            )
            conn.execute(
                "UPDATE confirmed_trip_snapshots SET is_current = FALSE WHERE trip_id = %s",
                (trip_id,),
            )
            # An itinerary belongs to the previously confirmed requirements.
            # Keep it as history, but never show it as the active result after
            # the user confirms materially changed trip input.
            conn.execute(
                "UPDATE trip_plans SET is_active = FALSE WHERE trip_id = %s",
                (trip_id,),
            )
            row = conn.execute(
                """
                INSERT INTO confirmed_trip_snapshots (
                    trip_id, version, fingerprint, payload, is_current, created_at
                ) VALUES (%s, %s, %s, %s, TRUE, %s)
                RETURNING *
                """,
                (
                    trip_id,
                    int(next_version_row["next_version"]),
                    fingerprint,
                    Json(canonical_payload),
                    now,
                ),
            ).fetchone()
            conn.execute(
                "UPDATE trips SET updated_at = %s WHERE id = %s", (now, trip_id)
            )
            conn.commit()
        return self._confirmed_snapshot_row_to_model(row)

    def get_current_confirmed_trip_snapshot(
        self,
        trip_id: int,
    ) -> ConfirmedTripSnapshotRow | None:
        """Load the only current immutable planning input for one trip."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM confirmed_trip_snapshots
                WHERE trip_id = %s AND is_current = TRUE
                ORDER BY version DESC, id DESC
                LIMIT 1
                """,
                (trip_id,),
            ).fetchone()
        if row is None:
            return None
        return self._confirmed_snapshot_row_to_model(row)

    def _connect(self) -> psycopg.Connection:
        """Open a PostgreSQL connection using dict-like rows."""

        conn = psycopg.connect(
            self.database_url,
            row_factory=dict_row,
            connect_timeout=10,
        )
        schema_name = self.schema or "public"
        try:
            with conn.cursor() as cursor:
                cursor.execute("SET statement_timeout TO '30s'")
                cursor.execute(
                    sql.SQL("SET search_path TO {}").format(
                        sql.Identifier(schema_name)
                    )
                )
        except Exception:
            conn.close()
            raise
        return conn

    @staticmethod
    def _refresh_sequence(conn: psycopg.Connection, table: str, column: str) -> None:
        """Move a serial sequence past any explicit ids inserted by bootstrap."""

        conn.execute(
            """
            SELECT setval(
                pg_get_serial_sequence(%s, %s),
                GREATEST((SELECT COALESCE(MAX(id), 0) FROM users), 1),
                TRUE
            )
            """,
            (table, column),
        )

    def _sync_trip_scalars_in_tx(
        self, conn: psycopg.Connection, trip_id: int, fields: dict
    ) -> None:
        """Update denormalized trip columns inside an open transaction."""

        filtered = {key: value for key, value in fields.items() if value is not None}
        if not filtered:
            return
        allowed_fields = {
            "status",
            "origin_city",
            "destination_type",
            "destination_items",
            "start_date",
            "end_date",
            "number_of_days",
            "travelers_adults",
            "travelers_children",
            "travelers_seniors",
            "budget_currency",
            "budget_amount",
            "trip_scope",
            "spending_tier",
            "travel_style",
            "trip_purpose",
            "slug",
            "trip_name",
            "trip_description",
        }
        updates = {
            key: value for key, value in filtered.items() if key in allowed_fields
        }
        if not updates:
            return
        updates["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = %s" for key in updates)
        conn.execute(
            f"UPDATE trips SET {assignments} WHERE id = %s",
            (*updates.values(), trip_id),
        )

    def _trip_scalar_fields_from_memory_json(self, agent_state_json: str) -> dict:
        """Extract the trip columns that are mirrored from trip memory."""

        try:
            memory = load_runtime_memory(agent_state_json)
        except (ValidationError, json.JSONDecodeError, TypeError, ValueError):
            return {}
        destination = memory.needed.destination
        return {
            "status": memory.metadata.status.value,
            "origin_city": memory.needed.origin,
            "destination_type": destination.type.value if destination else None,
            "destination_items": json.dumps(destination.items, ensure_ascii=False)
            if destination
            else None,
            "start_date": memory.needed.start_date.isoformat()
            if memory.needed.start_date
            else None,
            "end_date": memory.needed.end_date.isoformat()
            if memory.needed.end_date
            else None,
            "number_of_days": memory.needed.trip_duration_days,
            "travelers_adults": memory.needed.num_adults,
            "travelers_children": memory.optional.num_children,
            "travelers_seniors": memory.optional.num_seniors,
            "budget_currency": memory.metadata.currency_code,
            "budget_amount": memory.needed.trip_budget,
            "trip_scope": memory.metadata.trip_scope.value
            if memory.metadata.trip_scope
            else None,
            "spending_tier": memory.optional.spending_tier.value
            if memory.optional.spending_tier
            else None,
            "travel_style": memory.optional.travel_style.value
            if memory.optional.travel_style
            else None,
            "trip_purpose": memory.optional.trip_purpose.value
            if memory.optional.trip_purpose
            else None,
            "slug": memory.metadata.slug,
            "trip_name": memory.metadata.trip_name,
            "trip_description": memory.metadata.trip_description,
        }

    def _normalize_artifact_type(self, artifact_type: str) -> str:
        """Map legacy artifact names to the current canonical names."""

        if artifact_type == "itinerary":
            return PlanningArtifactType.DETAIL_FILL.value
        return artifact_type

    def _next_artifact_version(self, trip_id: int) -> int:
        """Return the next artifact version number for one trip."""

        with self._connect() as conn:
            return self._next_artifact_version_in_tx(conn, trip_id)

    @staticmethod
    def _next_artifact_version_in_tx(conn: psycopg.Connection, trip_id: int) -> int:
        """Return the next artifact version using an existing connection."""

        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM planning_artifacts WHERE trip_id = %s",
            (trip_id,),
        ).fetchone()
        return int((row["version"] if row is not None else 0) or 0) + 1

    def _get_latest_artifact_row(
        self, trip_id: int, artifact_type: str
    ) -> dict[str, Any] | None:
        """Return the raw latest artifact row for one trip and type."""

        normalized_type = self._normalize_artifact_type(artifact_type)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM planning_artifacts
                WHERE trip_id = %s AND artifact_type = %s
                ORDER BY version DESC, id DESC
                LIMIT 1
                """,
                (trip_id, normalized_type),
            ).fetchone()
        return row

    @staticmethod
    def _decode_json(value: Any) -> Any:
        """Decode a JSON column value returned by either text or JSONB drivers."""

        if value is None:
            return {}
        if isinstance(value, str):
            return json.loads(value or "{}")
        return value

    @staticmethod
    def _as_text(value: Any) -> str | None:
        """Return storage values as text for Pydantic row models."""

        if value is None or isinstance(value, str):
            return value
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    @classmethod
    def _artifact_row_to_model(cls, row: dict[str, Any] | None) -> ArtifactRow | None:
        """Convert one artifact row into a typed artifact record."""

        if row is None:
            return None
        return ArtifactRow(
            id=int(row["id"]),
            trip_id=int(row["trip_id"]),
            artifact_type=row["artifact_type"],
            data=cls._decode_json(row["data"]),
            is_stale=bool(row["is_stale"]),
            is_current=bool(row.get("is_current", not row["is_stale"])),
            version=int(row["version"]),
            created_at=cls._as_text(row["created_at"]),
        )

    @classmethod
    def _confirmed_snapshot_row_to_model(
        cls,
        row: dict[str, Any] | None,
    ) -> ConfirmedTripSnapshotRow:
        """Convert a raw immutable planning-input row into its typed form."""

        if row is None:
            raise RepositoryError(
                "confirmed_trip_snapshot", RuntimeError("Snapshot row missing")
            )
        return ConfirmedTripSnapshotRow(
            id=int(row["id"]),
            trip_id=int(row["trip_id"]),
            version=int(row["version"]),
            fingerprint=str(row["fingerprint"]),
            payload=cls._decode_json(row["payload"]),
            is_current=bool(row["is_current"]),
            created_at=cls._as_text(row["created_at"]),
        )

    @classmethod
    def _message_row_to_model(cls, row: dict[str, Any] | None) -> MessageRow | None:
        """Convert one message row into a typed message record."""

        if row is None:
            return None
        return MessageRow(
            id=int(row["id"]),
            trip_id=int(row["trip_id"]),
            sender_id=row["sender_id"],
            role=row["role"],
            content=row["content"],
            meta=cls._decode_json(row["meta"] or {}),
            created_at=cls._as_text(row["created_at"]),
        )

    @classmethod
    def _trip_row_to_model(cls, row: dict[str, Any] | None) -> TripRow | None:
        """Convert one trip row into a typed trip record."""

        if row is None:
            return None
        data = dict(row)
        for key in ("start_date", "end_date", "created_at", "updated_at"):
            data[key] = cls._as_text(data.get(key))
        if data.get("agent_state") is not None and not isinstance(
            data["agent_state"], str
        ):
            data["agent_state"] = json.dumps(data["agent_state"], ensure_ascii=False)
        if data.get("destination_items") is not None and not isinstance(
            data["destination_items"], str
        ):
            data["destination_items"] = json.dumps(
                data["destination_items"], ensure_ascii=False
            )
        if isinstance(data.get("budget_amount"), Decimal):
            data["budget_amount"] = int(data["budget_amount"])
        if data.get("budget_currency") is None:
            data["budget_currency"] = "INR"
        if data.get("factors") is None:
            data["factors"] = []
        elif isinstance(data["factors"], str):
            try:
                data["factors"] = json.loads(data["factors"])
            except Exception:
                data["factors"] = []
        return TripRow.model_validate(data)

    @classmethod
    def _user_row_to_model(cls, row: dict[str, Any] | None) -> UserRow | None:
        """Convert one user row into a typed user record."""

        if row is None:
            return None
        data = dict(row)
        if data.get("dob") is not None:
            data["dob"] = str(data["dob"])
        data["created_at"] = cls._as_text(data.get("created_at"))
        data["updated_at"] = cls._as_text(data.get("updated_at"))
        return UserRow.model_validate(data)

    def create_user_session(
        self, user_id: int, token_hash: str, expires_at_iso: str
    ) -> None:
        """Store an active login session token."""
        now = utc_now()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_sessions (user_id, session_token_hash, expires_at, created_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (session_token_hash) DO NOTHING
                """,
                (user_id, token_hash, expires_at_iso, now),
            )
            conn.commit()

    def get_user_by_session_token(self, token_hash: str) -> UserRow | None:
        """Return the user associated with a valid active session token."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT u.* FROM users u
                JOIN user_sessions s ON u.id = s.user_id
                WHERE s.session_token_hash = %s AND s.expires_at > (now() AT TIME ZONE 'utc')
                LIMIT 1
                """,
                (token_hash,),
            ).fetchone()
        return self._user_row_to_model(row)

    def delete_user_session(self, token_hash: str) -> None:
        """Revoke one active session token."""
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM user_sessions WHERE session_token_hash = %s",
                (token_hash,),
            )
            conn.commit()

    def update_user_profile(self, user_id: int, **fields: Any) -> UserRow | None:
        """Update editable profile fields for a user."""
        allowed = {
            "name",
            "username",
            "phone_number",
            "dob",
            "gender",
            "citizenship",
            "spending_tier",
            "home_city",
        }
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return self.get_user(user_id)

        now = utc_now()
        set_clauses = [f"{k} = %s" for k in updates]
        set_clauses.append("updated_at = %s")
        params = list(updates.values()) + [now, user_id]

        query = f"UPDATE users SET {', '.join(set_clauses)} WHERE id = %s"
        with self._write_lock, self._connect() as conn:
            conn.execute(query, tuple(params))
            conn.commit()

        return self.get_user(user_id)

    def delete_trip(self, trip_id: int, owner_id: int) -> bool:
        """Delete one trip owned by a user."""
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM trips WHERE id = %s AND owner_id = %s",
                (trip_id, owner_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def save_trip_plan(
        self,
        trip_id: int,
        job_id: str | None,
        delivery_status: str,
        plan_json: JsonObject,
        markdown_summary: str,
    ) -> TripPlanRow:
        """Save a generated day-by-day travel plan."""
        now = utc_now()
        plan_id = f"plan_{uuid.uuid4().hex[:16]}"
        with self._write_lock, self._connect() as conn:
            # Mark prior plans inactive
            conn.execute(
                "UPDATE trip_plans SET is_active = FALSE WHERE trip_id = %s", (trip_id,)
            )
            version_row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS version FROM trip_plans WHERE trip_id = %s",
                (trip_id,),
            ).fetchone()
            version = int((version_row or {}).get("version", 1))
            row = conn.execute(
                """
                INSERT INTO trip_plans (
                    id, trip_id, job_id, version, delivery_status,
                    plan_json, markdown_summary, is_active, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s)
                RETURNING id, trip_id, job_id, version, delivery_status, plan_json, markdown_summary, is_active, created_at
                """,
                (
                    plan_id,
                    trip_id,
                    job_id,
                    version,
                    delivery_status,
                    Json(plan_json),
                    markdown_summary,
                    now,
                ),
            ).fetchone()
            conn.commit()
        return self._trip_plan_row_to_model(row)

    def get_active_trip_plan(self, trip_id: int) -> TripPlanRow | None:
        """Return the current active travel plan for a trip."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, trip_id, job_id, version, delivery_status,
                       plan_json, markdown_summary, is_active, created_at
                FROM trip_plans
                WHERE trip_id = %s AND is_active = TRUE
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (trip_id,),
            ).fetchone()
        return self._trip_plan_row_to_model(row)

    def _trip_plan_row_to_model(self, row: dict[str, Any] | None) -> TripPlanRow | None:
        if row is None:
            return None
        payload = dict(row)
        if isinstance(payload.get("plan_json"), str):
            try:
                payload["plan_json"] = json.loads(payload["plan_json"])
            except Exception:
                payload["plan_json"] = {}
        return TripPlanRow.model_validate(payload)
