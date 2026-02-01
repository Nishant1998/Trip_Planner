"""Publish truthful planner progress without depending on a UI transport."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol

from app.models.agent import ToolCallAudit
from app.models.progress import (
    PlanningStage,
    ProgressDetail,
    ProgressEvent,
    ProgressEventKind,
)
from app.models.plan import TripPlan


class ProgressSink(Protocol):
    async def publish(self, event: ProgressEvent) -> None: ...


class NullProgressSink:
    """Default sink for callers that do not need progress events."""

    async def publish(self, event: ProgressEvent) -> None:
        del event


class ProgressReporter:
    """Create ordered events and honest heartbeats for one planning run."""

    def __init__(
        self,
        run_id: str,
        sink: ProgressSink,
        *,
        heartbeat_seconds: float = 15,
        slow_stage_seconds: float = 45,
    ) -> None:
        self.run_id = run_id
        self.sink = sink
        self.heartbeat_seconds = heartbeat_seconds
        self.slow_stage_seconds = slow_stage_seconds
        self.current_stage = PlanningStage.STARTING
        self._sequence = 0
        self._publish_lock = asyncio.Lock()
        self.tool_calls: list[ToolCallAudit] = []

    @asynccontextmanager
    async def track(self, stage: PlanningStage) -> AsyncIterator[None]:
        """Publish start, periodic heartbeat, and successful completion."""
        self.current_stage = stage
        await self._publish(ProgressEventKind.STAGE_STARTED)
        stopped = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(stopped))
        succeeded = False
        try:
            yield
            succeeded = True
        finally:
            stopped.set()
            await heartbeat
            if succeeded:
                await self._publish(ProgressEventKind.STAGE_COMPLETED)

    async def waiting(self, detail: ProgressDetail) -> None:
        await self._publish(ProgressEventKind.WAITING, detail)

    async def retrying(self) -> None:
        await self._publish(
            ProgressEventKind.RETRYING,
            ProgressDetail.RECHECKING_THIS_STEP,
        )

    async def run_completed(self) -> None:
        await self._publish(ProgressEventKind.RUN_COMPLETED)

    async def run_failed(self) -> None:
        await self._publish(ProgressEventKind.RUN_FAILED)

    async def record_tool_call(self, audit: ToolCallAudit) -> None:
        """Keep a request-local audit and forward it when the sink supports it."""
        self.tool_calls.append(audit)
        record = getattr(self.sink, "record_tool_call", None)
        if record is not None:
            await record(audit)

    async def record_candidate(self, evaluation_id: str, plan: TripPlan) -> None:
        """Forward an internal checked candidate when the sink archives development runs."""

        record = getattr(self.sink, "record_candidate", None)
        if record is not None:
            await record(evaluation_id, plan)

    async def _heartbeat(self, stopped: asyncio.Event) -> None:
        started = monotonic()
        first_heartbeat = True
        while not await _wait(stopped, self.heartbeat_seconds):
            detail = (
                ProgressDetail.STEP_TAKING_LONGER
                if not first_heartbeat
                and monotonic() - started >= self.slow_stage_seconds
                else ProgressDetail.STILL_WORKING
            )
            await self._publish(ProgressEventKind.HEARTBEAT, detail)
            first_heartbeat = False

    async def _publish(
        self,
        kind: ProgressEventKind,
        detail: ProgressDetail | None = None,
    ) -> None:
        async with self._publish_lock:
            self._sequence += 1
            await self.sink.publish(
                ProgressEvent(
                    run_id=self.run_id,
                    sequence=self._sequence,
                    stage=self.current_stage,
                    kind=kind,
                    detail=detail,
                    occurred_at=datetime.now(UTC),
                )
            )


async def _wait(event: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(event.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True
