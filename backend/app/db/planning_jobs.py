"""Transactional persistence for planning execution and reconnects."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4
from psycopg.types.json import Json


def _delivery_status_for_persistence(final_validation):
    """Return a safe legacy fallback when an older caller lacks delivery status."""
    delivery = final_validation.get("delivery_status")
    if delivery is not None:
        return delivery
    validation = final_validation.get("status")
    if final_validation.get("publishable") is True:
        return (
            "READY" if validation == "PASS"
            else "READY_WITH_WARNINGS" if validation == "BEST_EFFORT"
            else None
        )
    if validation in {"PASS", "BEST_EFFORT"}:
        return "DRAFT_NEEDS_CONFIRMATION"
    return None


class PlanningJobPersistence:
    def get_latest_planning_job(self, trip_id):
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM planning_jobs WHERE trip_id = %s ORDER BY created_at DESC, id DESC LIMIT 1",
                (trip_id,),
            ).fetchone()

    def touch_planning_job(self, job_id):
        with self._connect() as conn:
            row = conn.execute(
                "UPDATE planning_jobs SET updated_at = %s WHERE id = %s AND status = 'running' RETURNING id",
                (datetime.now(UTC).isoformat(), job_id),
            ).fetchone()
            return row is not None

    def persist_planning_progress(self, job_id, progress):
        with self._connect() as conn:
            row = conn.execute(
                """UPDATE planning_jobs SET progress = %s,
                   progress_percent = GREATEST(progress_percent, %s), current_step = %s, updated_at = %s
                   WHERE id = %s AND status = 'running'
                   AND COALESCE((progress->>'sequence')::int, 0) < %s RETURNING id""",
                (
                    Json(progress),
                    min(99, progress["progress_percent"]),
                    progress["message"],
                    datetime.now(UTC).isoformat(),
                    job_id,
                    progress["sequence"],
                ),
            ).fetchone()
            if row:
                return True
            return (
                conn.execute(
                    "SELECT id FROM planning_jobs WHERE id = %s AND status = 'running'",
                    (job_id,),
                ).fetchone()
                is not None
            )

    def fail_planning_job(self, job_id, failure):
        with self._connect() as conn:
            row = conn.execute(
                """UPDATE planning_jobs SET status = 'failed', failure = %s, updated_at = %s
                   WHERE id = %s AND status IN ('queued', 'running') RETURNING trip_id, snapshot_id""",
                (Json(failure), datetime.now(UTC).isoformat(), job_id),
            ).fetchone()
            if row:
                conn.execute(
                    """UPDATE trips SET status = 'failed' WHERE id = %s AND EXISTS
                       (SELECT 1 FROM confirmed_trip_snapshots WHERE id = %s AND is_current)""",
                    (row["trip_id"], row["snapshot_id"]),
                )

    def expire_planning_jobs(self, lease_seconds):
        cutoff = datetime.now(UTC) - timedelta(seconds=lease_seconds)
        with self._connect() as conn:
            rows = conn.execute(
                """UPDATE planning_jobs SET status = 'failed', failure = %s, updated_at = %s
                   WHERE status = 'running' AND updated_at::timestamptz < %s
                   RETURNING trip_id, snapshot_id""",
                (
                    Json(
                        {
                            "code": "interrupted",
                            "reason": "Planning was interrupted. Please retry.",
                            "retryable": True,
                        }
                    ),
                    datetime.now(UTC).isoformat(),
                    cutoff,
                ),
            ).fetchall()
            for row in rows:
                conn.execute(
                    """UPDATE trips SET status = 'failed' WHERE id = %s AND EXISTS
                       (SELECT 1 FROM confirmed_trip_snapshots WHERE id = %s AND is_current)""",
                    (row["trip_id"], row["snapshot_id"]),
                )

    def complete_planning_job(self, job_id, payload):
        display = payload.get("display_trip")
        final_validation = payload.get("final_validation", {})
        validation = final_validation.get("status")
        delivery = _delivery_status_for_persistence(final_validation)
        if (
            not isinstance(display, dict)
            or not display.get("days")
            or validation not in {"PASS", "BEST_EFFORT"}
            or delivery not in {"READY", "READY_WITH_WARNINGS", "DRAFT_NEEDS_CONFIRMATION"}
        ):
            raise ValueError("Cannot persist an unvalidated itinerary")
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            job = conn.execute(
                "SELECT * FROM planning_jobs WHERE id = %s FOR UPDATE", (job_id,)
            ).fetchone()
            if job is None:
                raise LookupError("Planning job not found")
            if job["status"] == "completed":
                return
            if job["status"] != "running":
                raise ValueError("Planning job is no longer running")
            trip_id = job["trip_id"]
            conn.execute("SELECT id FROM trips WHERE id = %s FOR UPDATE", (trip_id,))
            current = conn.execute(
                "SELECT id FROM confirmed_trip_snapshots WHERE trip_id = %s AND is_current FOR UPDATE",
                (trip_id,),
            ).fetchone()
            if current is None or current["id"] != job["snapshot_id"]:
                raise ValueError("Cannot publish an itinerary for an outdated trip")
            version = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS version FROM trip_plans WHERE trip_id = %s",
                (trip_id,),
            ).fetchone()["version"]
            conn.execute(
                "UPDATE trip_plans SET is_active = FALSE WHERE trip_id = %s", (trip_id,)
            )
            conn.execute(
                """INSERT INTO trip_plans
                   (id, trip_id, job_id, version, delivery_status, plan_json, markdown_summary, is_active, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s)""",
                (
                    f"plan_{uuid4().hex[:16]}",
                    trip_id,
                    job_id,
                    version,
                    "ready" if delivery == "READY" else "ready_with_warnings"
                    if delivery == "READY_WITH_WARNINGS" else "draft_needs_confirmation",
                    Json(display),
                    display.get("trip_summary", {}).get("title", ""),
                    now,
                ),
            )
            for kind, key in (
                ("retrieval", "research_report"),
                ("detail_fill", "planning_result"),
                ("governance", "final_validation"),
            ):
                artifact_version = self._next_artifact_version_in_tx(conn, trip_id)
                conn.execute(
                    """INSERT INTO planning_artifacts (trip_id, artifact_type, data, is_stale, version, created_at)
                       VALUES (%s, %s, %s, FALSE, %s, %s)""",
                    (trip_id, kind, Json(payload[key]), artifact_version, now),
                )
            conn.execute(
                """UPDATE planning_jobs SET status = 'completed', result = %s, failure = NULL,
                   progress_percent = 100, current_step = 'Itinerary ready', updated_at = %s WHERE id = %s""",
                (Json(payload), now, job_id),
            )
            conn.execute(
                "UPDATE trips SET status = 'confirmed', updated_at = %s WHERE id = %s",
                (now, trip_id),
            )
