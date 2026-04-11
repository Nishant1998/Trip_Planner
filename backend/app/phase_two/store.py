"""PostgreSQL job lifecycle and narrowly scoped recovery records."""

import asyncio
import uuid
from datetime import UTC, datetime

from psycopg.types.json import Json

from .persistence import durable_plan
from app.services.demo_usage import lock_account, reserve_planning


def completed_job_status(row):
    """Read older saved jobs without treating plan quality as execution state."""
    if row is None:
        return None
    result = dict(row)
    saved_plan_id = result.pop("saved_plan_id", None)
    if (
        saved_plan_id
        and result["status"] == "failed"
        and (result.get("failure") or {}).get("code") == "storage_failed"
    ):
        result.update(
            status="completed", current_step="Saved", progress_percent=100,
            failure=None, result={**(result.get("result") or {}), "plan_id": saved_plan_id},
        )
    if (
        result["status"] in {"complete", "partial"}
        and result.get("progress_percent") == 100
        and isinstance(result.get("result"), dict)
        and result["result"].get("plan_id")
    ):
        result["status"] = "completed"
    return result


class PlanningStore:
    def __init__(self, repository):
        self.repository = repository

    async def create_job(self, trip_id, snapshot_id, retry=False):
        return await asyncio.to_thread(self._create_job, trip_id, snapshot_id, retry)

    def _create_job(self, trip_id, snapshot_id, retry):
        with self.repository._connect() as conn:
            trip = conn.execute('SELECT owner_id FROM trips WHERE id=%s', (trip_id,)).fetchone()
            if not trip:
                raise ValueError('Trip not found')
            account = lock_account(conn, trip['owner_id'])
            conn.execute("SELECT id FROM trips WHERE id=%s FOR UPDATE", (trip_id,))
            current = conn.execute(
                "SELECT id FROM confirmed_trip_snapshots WHERE trip_id=%s AND is_current=TRUE",
                (trip_id,),
            ).fetchone()
            if not current or str(current["id"]) != str(snapshot_id):
                raise ValueError("confirmed snapshot is stale")
            prior = conn.execute(
                "SELECT * FROM planning_jobs WHERE snapshot_id=%s ORDER BY created_at DESC, id DESC LIMIT 1",
                (snapshot_id,),
            ).fetchone()
            if prior:
                if prior["status"] in ("queued", "running"):
                    updated = datetime.fromisoformat(
                        prior["updated_at"].replace("Z", "+00:00")
                    )
                    if (datetime.now(UTC) - updated).total_seconds() < 360:
                        return dict(prior), False
                    conn.execute(
                        "UPDATE planning_jobs SET status='interrupted' WHERE id=%s",
                        (prior["id"],),
                    )
                elif not retry:
                    return completed_job_status(prior), False
            run_id = str(uuid.uuid4())
            reserve_planning(conn, account, trip_id, run_id)
            row = conn.execute(
                "INSERT INTO planning_jobs (id,trip_id,snapshot_id,status) VALUES (%s,%s,%s,'queued') RETURNING *",
                (run_id, trip_id, snapshot_id),
            ).fetchone()
            conn.commit()
            return dict(row), True

    async def status(self, trip_id):
        def read():
            with self.repository._connect() as conn:
                row = conn.execute(
                    """SELECT j.id,j.status,j.progress_percent,j.current_step,
                              j.result,j.failure,j.updated_at,p.id AS saved_plan_id
                       FROM planning_jobs j
                       LEFT JOIN confirmed_trip_snapshots s
                         ON s.id=j.snapshot_id AND s.is_current=TRUE
                       LEFT JOIN trip_plans p
                         ON p.job_id=j.id AND p.is_active=TRUE AND s.id IS NOT NULL
                       WHERE j.trip_id=%s
                       ORDER BY j.created_at DESC,j.id DESC LIMIT 1""",
                    (trip_id,),
                ).fetchone()
                return completed_job_status(row)

        return await asyncio.to_thread(read)

    async def update(self, run_id, status, step, percent, result=None, failure=None):
        def write():
            with self.repository._connect() as conn:
                conn.execute(
                    "UPDATE planning_jobs SET status=%s,current_step=%s,progress_percent=%s,result=%s,failure=%s,updated_at=%s WHERE id=%s",
                    (
                        status,
                        step,
                        percent,
                        Json(result),
                        Json(failure),
                        datetime.now(UTC).isoformat(),
                        run_id,
                    ),
                )
                conn.commit()

        await asyncio.to_thread(write)

    async def save_checkpoint(self, run_id, checkpoint, metadata):
        def write():
            with self.repository._connect() as conn:
                conn.execute(
                    "INSERT INTO phase_two_control_checkpoints VALUES (%s,%s,%s) ON CONFLICT (run_id) DO UPDATE SET checkpoint=EXCLUDED.checkpoint,metadata=EXCLUDED.metadata",
                    (run_id, Json(checkpoint), Json(metadata)),
                )
                conn.commit()

        await asyncio.to_thread(write)

    async def read_collection(self, snapshot_id, key):
        def read():
            with self.repository._connect() as conn:
                row = conn.execute(
                    "SELECT payload FROM phase_two_collections WHERE snapshot_id=%s AND request_hash=%s",
                    (snapshot_id, key),
                ).fetchone()
                return row["payload"] if row else None

        return await asyncio.to_thread(read)

    async def write_collection(self, snapshot_id, key, value):
        def write():
            with self.repository._connect() as conn:
                conn.execute(
                    "INSERT INTO phase_two_collections VALUES (%s,%s,%s) ON CONFLICT (snapshot_id,request_hash) DO UPDATE SET payload=EXCLUDED.payload",
                    (snapshot_id, key, Json(value)),
                )
                conn.commit()

        await asyncio.to_thread(write)

    async def save_plan(self, plan, snapshot_id):
        # Lock the trip until insert completes: a re-confirmation cannot publish
        # an old plan over a newly confirmed snapshot.
        def save():
            with self.repository._connect() as conn:
                conn.execute(
                    "SELECT id FROM trips WHERE id=%s FOR UPDATE", (plan.trip_id,)
                )
                row = conn.execute(
                    "SELECT id FROM confirmed_trip_snapshots WHERE trip_id=%s AND is_current=TRUE",
                    (plan.trip_id,),
                ).fetchone()
                if not row or str(row["id"]) != str(snapshot_id):
                    raise ValueError("cannot publish a stale snapshot")
                conn.execute(
                    "UPDATE trip_plans SET is_active=FALSE WHERE trip_id=%s",
                    (plan.trip_id,),
                )
                version = conn.execute(
                    "SELECT COALESCE(MAX(version),0)+1 AS v FROM trip_plans WHERE trip_id=%s",
                    (plan.trip_id,),
                ).fetchone()["v"]
                conn.execute(
                    "INSERT INTO trip_plans (id,trip_id,job_id,version,delivery_status,plan_json,markdown_summary,is_active) VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE)",
                    (
                        plan.plan_id,
                        plan.trip_id,
                        plan.run_id,
                        version,
                        plan.status,
                        Json(durable_plan(plan)),
                        "",
                    ),
                )
                conn.commit()
            return plan.plan_id

        return await asyncio.to_thread(save)
