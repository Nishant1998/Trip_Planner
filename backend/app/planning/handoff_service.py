"""Durable execution of confirmed trip snapshots."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from uuid import uuid4

from app.config import Settings
from app.models.failure import FailureCode
from app.models.input import TripMemoryInput
from app.planner_module.models.research import ResearchReport
from app.services.failure import FailureMapper
from app.services.llm_usage import capture_llm_usage
from app.services.planning_queue import PlanningJobQueue
from app.services.traveler_context import build_traveler_context

logger = logging.getLogger(__name__)


class ConfirmedPlanningHandoffService:
    def __init__(
        self,
        repository,
        planner,
        event_bus,
        job_queue=None,
        run_jobs_inline=True,
        settings=None,
    ):
        self.repository = repository
        self.planner = planner
        self.event_bus = event_bus
        self.job_queue = job_queue or PlanningJobQueue(None)
        self.run_jobs_inline = run_jobs_inline
        self.settings = settings or Settings.from_env()
        self._watchers = {}
        self._starting = asyncio.Lock()

    async def _db(self, name, *args):
        return await asyncio.to_thread(getattr(self.repository, name), *args)

    async def start_for_trip(self, trip_id):
        job = await self.queue_for_trip(trip_id)
        if job is None:
            return None
        if self.run_jobs_inline and job.status == "queued":
            await self.execute_job(job.id)
        return job.id

    async def queue_for_trip(self, trip_id):
        snapshot = await self._db("get_current_confirmed_trip_snapshot", trip_id)
        if snapshot is None:
            return None
        job = await self._db(
            "create_or_get_planning_job",
            trip_id,
            snapshot.id,
            f"plan_{uuid4().hex[:16]}",
        )
        if job.status == "queued":
            await self.job_queue.enqueue(job.id)
            await self._publish_status(trip_id, "queued", "Trip planning queued")
        return job

    async def execute_job(self, job_id):
        async with self._starting:
            if (
                job_id in self._watchers
                or len(self._watchers) >= self.settings.max_concurrent_planning_jobs
            ):
                return False
            job = await self._db("claim_queued_planning_job", job_id)
            if job is None:
                return False
            task = asyncio.create_task(self._execute(job))
            self._watchers[job_id] = task
            task.add_done_callback(self._task_finished)
            return True

    @staticmethod
    def _task_finished(task):
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "planning_persistence_failed",
                extra={"error_type": type(task.exception()).__name__},
            )

    async def resume_after_restart(self):
        # An interrupted paid run expires visibly, allowing an explicit retry.
        await self._db("expire_planning_jobs", self.settings.planning_lease_seconds)
        for job in await self._db("list_resumable_planning_jobs"):
            if job.status == "queued":
                if self.run_jobs_inline:
                    await self.execute_job(job.id)
                else:
                    await self.job_queue.enqueue(job.id)

    async def _execute(self, job):
        heartbeat = None
        try:
            snapshot = await self._db(
                "get_current_confirmed_trip_snapshot", job.trip_id
            )
            if snapshot is None or snapshot.id != job.snapshot_id:
                raise ValueError("Confirmed trip changed before planning started")
            memory = TripMemoryInput.model_validate(snapshot.payload)
            trip = await self._db("get_trip", job.trip_id)
            # Older repository adapters can execute a confirmed trip without a
            # profile lookup. Profile context enriches planning when available;
            # it is never required to run a valid confirmed trip.
            get_user = getattr(self.repository, "get_user", None)
            user = (
                await asyncio.to_thread(get_user, trip.owner_id)
                if callable(get_user)
                else None
            )
            await self._db("update_trip_status", job.trip_id, "planning")
            logger.info(
                "planning_job_started",
                extra={"run_id": job.id, "job_id": job.id, "trip_id": job.trip_id},
            )
            heartbeat = asyncio.create_task(self._heartbeat(job.id))
            service = self

            saved_report = None
            get_saved_report = getattr(self.repository, "get_current_research_report", None)
            if callable(get_saved_report):
                artifact = await asyncio.to_thread(
                    get_saved_report, job.trip_id, snapshot.id
                )
                payload = getattr(artifact, "data", {}) if artifact else {}
                candidate = payload.get("report") if isinstance(payload, dict) else None
                if candidate:
                    report = ResearchReport.model_validate(candidate)
                    if report.status != "blocked" and report.quality.sufficient_for_planning:
                        saved_report = report
                        logger.info(
                            "planning_reusing_research_report",
                            extra={"run_id": job.id, "job_id": job.id, "trip_id": job.trip_id},
                        )

            async def save_research_report(report: ResearchReport) -> None:
                save_report = getattr(self.repository, "save_current_research_report", None)
                if not callable(save_report):
                    return
                report_data = report.model_dump(mode="json")
                metadata = {
                    "status": report.status,
                    "sufficient_for_planning": report.quality.sufficient_for_planning,
                    "architecture_version": report.architecture_version,
                    "prompt_version": report.prompt_version,
                    "provider_requests": report.metrics.provider_requests,
                    "llm_calls": report.metrics.llm_calls,
                    "llm_input_tokens": report.metrics.llm_input_tokens,
                    "llm_output_tokens": report.metrics.llm_output_tokens,
                    "unresolved_gaps": report.unresolved_gaps,
                    "saved_at": datetime.now(UTC).isoformat(),
                }
                artifact_id = await asyncio.to_thread(
                    save_report, job.trip_id, snapshot.id, report_data, metadata
                )
                logger.info(
                    "research_report_saved",
                    extra={
                        "run_id": job.id,
                        "job_id": job.id,
                        "trip_id": job.trip_id,
                        "research_artifact_id": artifact_id,
                        "research_status": report.status,
                    },
                )

            class Sink:
                async def publish(self, event):
                    progress = event.model_dump(mode="json")
                    if not await service._db(
                        "persist_planning_progress", job.id, progress
                    ):
                        raise RuntimeError("Planning job is no longer active")
                    await service.event_bus.publish(
                        job.trip_id,
                        "planning_status",
                        {
                            **progress,
                            "status": event.stage.value,
                        },
                    )

            with capture_llm_usage(
                self.repository,
                user_id=trip.owner_id,
                trip_id=job.trip_id,
                phase="planning",
                force_new=True,
            ):
                async with asyncio.timeout(self.settings.timeout_seconds):
                    if saved_report is not None:
                        await self._publish_status(
                            job.trip_id,
                            "research_completed",
                            "Reusing saved research. Building your itinerary.",
                        )
                        result = await self.planner.generate_from_research_report(
                            saved_report, request_id=job.id, progress_sink=Sink()
                        )
                    else:
                        result = await self.planner.generate(
                            memory,
                            request_id=job.id,
                            progress_sink=Sink(),
                            traveler_context=build_traveler_context(user),
                            research_report_callback=save_research_report,
                        )
            await self._db(
                "complete_planning_job", job.id, result.model_dump(mode="json")
            )
            logger.info(
                "planning_job_completed",
                extra={"run_id": job.id, "job_id": job.id, "trip_id": job.trip_id},
            )
            delivery = getattr(result, "final_validation", {}).get("delivery_status")
            await self._publish_status(
                job.trip_id,
                "plan_ready" if delivery in {"READY", "READY_WITH_WARNINGS"} else "plan_draft",
                "Your trip plan is ready." if delivery in {"READY", "READY_WITH_WARNINGS"}
                else "Your trip draft needs confirmation for unresolved details.",
            )
        except asyncio.CancelledError:
            await self._db(
                "fail_planning_job",
                job.id,
                {
                    "code": "cancelled",
                    "reason": "Planning was interrupted. Please retry.",
                    "retryable": True,
                },
            )
            raise
        except Exception as error:
            code = (
                FailureCode.PLANNING_TIMEOUT
                if isinstance(error, TimeoutError)
                else None
            )
            mapped = FailureMapper().map(error, run_id=job.id, stage=None, code=code)
            logger.exception(
                "planning_failed",
                extra={
                    "run_id": job.id,
                    "job_id": job.id,
                    "trip_id": job.trip_id,
                    "error_type": type(error).__name__,
                },
            )
            await self._db(
                "fail_planning_job", job.id, mapped.public.model_dump(mode="json")
            )
            await self._publish_status(
                job.trip_id, "planning_failed", mapped.public.reason
            )
        finally:
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            self._watchers.pop(job.id, None)

    async def _heartbeat(self, job_id):
        while True:
            await asyncio.sleep(self.settings.planning_heartbeat_seconds)
            try:
                active = await self._db("touch_planning_job", job_id)
            except Exception as error:
                logger.exception(
                    "planning_heartbeat_failed",
                    extra={"run_id": job_id, "error_type": type(error).__name__},
                )
                active = False
            if not active:
                task = self._watchers.get(job_id)
                if task:
                    task.cancel()
                return

    async def close(self):
        tasks = list(self._watchers.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.job_queue.close()

    async def _publish_status(self, trip_id, status, message):
        await self.event_bus.publish(
            trip_id, "planning_status", {"status": status, "message": message}
        )
