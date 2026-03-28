"""Runtime service that coordinates one live collection turn at a time."""

from __future__ import annotations

import asyncio
from time import perf_counter
import os
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Awaitable, Callable
from typing import Any

from app.exceptions import RiyaRuntimeError, TurnTimeoutError
from app.models.api import (
    ErrorPayload,
    StateChangePayload,
    StatusEventPayload,
    TraceEventPayload,
)
from app.models.enums import LiveStatusPhase, TripStatus
from app.collection.orchestrator import TripOrchestrator
from app.collection.turn_io import TurnOutcome
from app.services.events import TripEventBus
from app.services.agent_timing import record_trip_turn_timing
from app.db.repository import TripRepository

logger = logging.getLogger(__name__)
trace_logger = logging.getLogger("uvicorn.error")

# ===== Constants =====

USER_MESSAGE_META = {"agent": "user", "stage": "collecting"}
STREAM_DELAY_SECONDS = 0.01
TURN_TIMEOUT_SECONDS = 120.0
# Sets a character limit for trace payload previews to prevent full memory dumps from being serialised.
TRACE_VALUE_LIMIT = 1600


# ===== Data Models =====


@dataclass(slots=True)
class TripBusyError(Exception):
    """Raised when a trip already has an active background turn."""

    message: str = "Trip is already processing another request."


# ===== Helpers =====


class TripEventPublisher:
    """Emit live collection events for one trip session."""

    def __init__(self, event_bus: TripEventBus, trip_id: int) -> None:
        self.event_bus = event_bus
        self.trip_id = trip_id
        self.trace_enabled = (
            os.getenv("RIYA_AUTH_MODE", "google").strip().lower() == "development"
        )

    async def publish_status(self, status: str, message: str | None = None) -> None:
        """Publish a status update for the current trip."""

        payload = StatusEventPayload(status=status, message=message or "")
        await self._publish("status", payload.model_dump())

    async def publish_state_change(self, old_status: str, new_status: str) -> None:
        """Publish a state transition for the current trip."""

        payload = StateChangePayload(
            old_status=old_status,
            new_status=new_status,
        )
        await self._publish("state_change", payload.model_dump())

    async def publish_checkpoint(self, payload: dict[str, Any]) -> None:
        """Publish a checkpoint payload to the frontend."""

        await self._publish("checkpoint", payload)

    async def publish_error(self, message: str, message_id: int | None = None) -> None:
        """Publish an error event for the current trip."""

        payload = ErrorPayload(message=message)
        data = payload.model_dump()
        if message_id is not None:
            data["message_id"] = str(message_id)
        await self._publish("error_event", data)

    # Implements publish_trace on TripEventPublisher so orchestration code can emit structured debug events to both the SSE stream and the server log.
    async def publish_trace(
        self,
        block: str,
        stage: str,
        *,
        input_data: Any = None,
        output_data: Any = None,
        message: str = "",
    ) -> None:
        """Publish a compact entry/exit/error trace for local debugging."""

        if not self.trace_enabled:
            return

        payload = TraceEventPayload(
            timestamp=datetime.now(timezone.utc).isoformat(),
            block=block,
            stage=stage,
            input=_compact_trace_value(input_data),
            output=_compact_trace_value(output_data),
            message=message,
        )
        trace_logger.debug("collection_trace", extra={"stage": stage, "block": block})
        await self._publish("trace", payload.model_dump())

    async def stream_text(self, text: str, message_id: int | None = None) -> None:
        """Stream assistant text in small chunks for a live typing effect."""

        for chunk in self._chunk_text(text):
            payload = {"chunk": chunk}
            if message_id is not None:
                payload["message_id"] = str(message_id)
            await self._publish("stream", payload)
            await asyncio.sleep(STREAM_DELAY_SECONDS)
        await self.publish_stream_end()

    async def publish_stream_text(self, text: str) -> None:
        """Publish the latest assistant text from a real streaming LLM run."""

        await self._publish("stream", {"text": text, "replace": True})

    async def publish_stream_chunk(self, chunk: str) -> None:
        """Publish one append-only assistant text chunk from a real LLM stream."""

        await self._publish("stream", {"chunk": chunk})

    async def publish_stream_end(self) -> None:
        """Publish the end marker for the current assistant stream."""

        await self._publish("stream_end", {})

    async def publish_suggestions(self, suggestions: list[str]) -> None:
        """Publish optional reply chips for the latest assistant turn."""

        await self._publish("suggestions", {"suggestions": suggestions})

    async def _publish(self, event_name: str, payload: dict[str, Any]) -> None:
        """Publish one event payload to the shared trip event bus."""

        await self.event_bus.publish(self.trip_id, event_name, payload)

    @staticmethod
    def _chunk_text(text: str) -> list[str]:
        """Split assistant text into short word chunks for streaming."""

        if not text.strip():
            return [""]
        return re.findall(r"\S+\s*|\s+", text)


# Normalizes any trace value into a small JSON-safe payload, truncating large objects so they fit in the browser debug panel.
def _compact_trace_value(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    raw = json.dumps(value, ensure_ascii=False, default=str)
    if len(raw) > TRACE_VALUE_LIMIT:
        return {"preview": raw[:TRACE_VALUE_LIMIT] + "...", "truncated": True}
    return json.loads(raw)


# ===== Core Logic =====


class CollectionRuntimeService:
    """Run one collection turn at a time and publish live events."""

    def __init__(
        self,
        repository: TripRepository,
        event_bus: TripEventBus,
        agents: Any,
        on_trip_confirmed: Callable[[int], Awaitable[object]] | None = None,
    ) -> None:
        self.repository = repository
        self.event_bus = event_bus
        self.orchestrator = TripOrchestrator(repository, agents)
        self.on_trip_confirmed = on_trip_confirmed
        self._active_tasks: dict[int, asyncio.Task[None]] = {}

    async def bootstrap_trip(self, trip_id: int) -> str:
        """Bootstrap a trip before normal message handling starts."""

        return await self.orchestrator.bootstrap_trip(trip_id)

    async def submit_user_message(
        self,
        trip_id: int,
        text: str,
        user_id: int = 1,
        client_message_id: str | None = None,
    ) -> None:
        """Queue one user message for asynchronous turn processing."""

        # Wraps submit_user_message with enter/exit/error traces so the trace panel shows message acceptance before background processing begins.
        publisher = TripEventPublisher(self.event_bus, trip_id)
        await publisher.publish_trace(
            "runtime.submit_user_message",
            "enter",
            input_data={"trip_id": trip_id, "text": text},
        )
        try:
            self._ensure_trip_is_idle(trip_id)
            self._store_user_message(
                trip_id=trip_id,
                text=text,
                user_id=user_id,
                client_message_id=client_message_id,
            )
            self._track_turn_task(trip_id, text, user_id)
        except Exception as exc:
            await publisher.publish_trace(
                "runtime.submit_user_message",
                "error",
                output_data={"error": str(exc)},
            )
            raise
        await publisher.publish_trace(
            "runtime.submit_user_message",
            "exit",
            output_data={"accepted": True, "queued": True},
        )

    def is_processing(self, trip_id: int) -> bool:
        """Return True when a trip already has an active background task."""

        task = self._active_tasks.get(trip_id)
        return task is not None and not task.done()

    async def close(self) -> None:
        """Close long-lived collection providers after active turns finish."""

        active = [task for task in self._active_tasks.values() if not task.done()]
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        close_orchestrator = getattr(self.orchestrator, "close", None)
        if callable(close_orchestrator):
            await close_orchestrator()
        close = getattr(self.orchestrator.agents, "close", None)
        if callable(close):
            await close()

    def _ensure_trip_is_idle(self, trip_id: int) -> None:
        """Raise when a trip already has an active background task."""

        if self.is_processing(trip_id):
            raise TripBusyError()

    def _store_user_message(
        self,
        trip_id: int,
        text: str,
        user_id: int,
        client_message_id: str | None = None,
    ) -> None:
        """Persist the user message before background processing starts."""

        self.repository.insert_message(
            trip_id,
            "user",
            text,
            sender_id=user_id,
            meta={
                **USER_MESSAGE_META,
                **(
                    {"client_message_id": client_message_id}
                    if client_message_id
                    else {}
                ),
            },
        )

    def _track_turn_task(self, trip_id: int, text: str, user_id: int) -> None:
        """Start and track the background task for one collection turn."""

        task = asyncio.create_task(self._run_turn(trip_id, text, user_id))
        self._active_tasks[trip_id] = task
        task.add_done_callback(lambda _: self._active_tasks.pop(trip_id, None))

    async def _run_turn(self, trip_id: int, text: str, user_id: int) -> None:
        """Run one collection turn and stream updates to the frontend."""

        publisher = TripEventPublisher(self.event_bus, trip_id)
        started_at = perf_counter()
        failure_type: str | None = None
        # Wraps the full turn execution with enter/exit trace events so the debug panel shows every turn boundary.
        # The outer run_turn trace guarantees every turn has an entry and exit or error event.
        await publisher.publish_trace(
            "runtime.run_turn",
            "enter",
            input_data={"trip_id": trip_id, "text": text},
        )
        try:
            outcome = await asyncio.wait_for(
                self.orchestrator.handle_message(
                    trip_id=trip_id,
                    user_message=text,
                    user_id=user_id,
                    publisher=publisher,
                ),
                timeout=TURN_TIMEOUT_SECONDS,
            )
            if (
                outcome.status is TripStatus.CONFIRMED
                and outcome.branch == "confirm"
                and self.on_trip_confirmed is not None
            ):
                await self.on_trip_confirmed(trip_id)
            await self._publish_successful_turn(publisher, outcome)
            # Emits a turn exit trace with status, branch, and response text after successful turn completion.
            await publisher.publish_trace(
                "runtime.run_turn",
                "exit",
                output_data={
                    "status": outcome.status.value,
                    "branch": outcome.branch,
                    "response_text": outcome.response_text,
                },
            )
        except asyncio.TimeoutError:
            failure_type = "TimeoutError"
            await self._publish_turn_failure(
                publisher,
                TurnTimeoutError(TURN_TIMEOUT_SECONDS),
                trip_id=trip_id,
            )
        except (RiyaRuntimeError, ValueError) as exc:
            failure_type = type(exc).__name__
            await self._publish_turn_failure(publisher, exc, trip_id=trip_id)
        except Exception as exc:  # pragma: no cover - defensive runtime guard
            failure_type = type(exc).__name__
            await self._publish_turn_failure(publisher, exc, trip_id=trip_id)
        finally:
            record_trip_turn_timing(
                user_id=user_id,
                trip_id=trip_id,
                elapsed_ms=int((perf_counter() - started_at) * 1000),
                success=failure_type is None,
                error_type=failure_type,
            )

    async def _publish_successful_turn(
        self,
        publisher: TripEventPublisher,
        outcome: TurnOutcome,
    ) -> None:
        """Publish the normal event sequence for a completed turn."""

        if outcome.checkpoint_payload is not None:
            await publisher.publish_checkpoint(outcome.checkpoint_payload)

        if outcome.error_message is not None:
            # The assistant retry text is already persisted to storage by the
            # orchestrator, so the frontend should show it once via error_event
            # instead of also streaming a duplicate assistant message.
            await publisher.publish_error(outcome.error_message, message_id=outcome.response_message_id)
            await publisher.publish_status(LiveStatusPhase.IDLE.value, "")
            return

        if outcome.response_streamed:
            await publisher.publish_stream_end()
        else:
            # A synthetic stream begins only after the response is complete.
            # Hide the temporary processing label before showing any reply text.
            await publisher.publish_status(LiveStatusPhase.IDLE.value, "")
            await publisher.stream_text(outcome.response_text, message_id=outcome.response_message_id)
        await publisher.publish_suggestions(outcome.suggestions)
        await publisher.publish_status(LiveStatusPhase.IDLE.value, "")

    async def _publish_turn_failure(
        self,
        publisher: TripEventPublisher,
        exc: Exception,
        *,
        trip_id: int,
    ) -> None:
        """Publish the standard error sequence for a failed turn."""

        logger.exception("Collection flow failed")
        # Emits an error trace event with the exception type when a turn fails so the debug panel shows the failure.
        # Show the failing outer block in the live trace panel.
        await publisher.publish_trace(
            "runtime.run_turn",
            "error",
            output_data={"error_type": type(exc).__name__, "error": str(exc)},
        )
        message = "Sorry, I didn't catch that cleanly. Could you say it once more in a simple way?"
        # Persist failures as assistant messages. SSE connections may begin after
        # a fast failure, and the messages endpoint remains the reliable replay.
        message_id = self.repository.insert_message(
            trip_id,
            "assistant",
            message,
            meta={"branch": "error", "error_type": type(exc).__name__},
        )
        await publisher.publish_error(message, message_id=message_id)
        await publisher.publish_status(
            LiveStatusPhase.ERROR.value,
            "I need one more try to understand that properly.",
        )
        await publisher.publish_stream_end()
        await publisher.publish_status(LiveStatusPhase.IDLE.value, "")
