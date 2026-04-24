"""Async event bus helpers for Server-Sent Events delivery."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque

from app.models.api import JsonObject

TRACE_HISTORY_LIMIT = 200
TRACE_QUEUE_SIZE = 300
MAX_TRACKED_TRIPS = 2_000


# ===== Helpers =====


def format_sse(event_name: str, payload: JsonObject) -> str:
    """Serialize one event payload into SSE wire format."""

    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ===== Core Logic =====


class TripEventBus:
    """Manage per-trip subscriber queues for live SSE delivery."""

    def __init__(self) -> None:
        self._subscribers: dict[int, set[asyncio.Queue[str]]] = defaultdict(set)
        # In-memory debug telemetry is deliberately session scoped.
        self._trace_history: dict[int, deque[str]] = defaultdict(
            lambda: deque(maxlen=TRACE_HISTORY_LIMIT)
        )
        self._latest_status: dict[int, str] = {}
        self._trace_sequences: dict[int, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def subscribe(self, trip_id: int) -> asyncio.Queue[str]:
        """Create and register a queue for one trip subscriber."""

        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=TRACE_QUEUE_SIZE)
        async with self._lock:
            self._prune_inactive_locked()
            # A trip can be initialized before the UI has navigated to its slug.
            # Replay the current trip-detail collection status to that late SSE subscriber.
            latest_status = self._latest_status.get(trip_id)
            if latest_status:
                queue.put_nowait(latest_status)
            # Replay trace history atomically before new events arrive.
            for message in self._trace_history.get(trip_id, ()):
                queue.put_nowait(message)
            self._subscribers[trip_id].add(queue)
        return queue

    async def unsubscribe(self, trip_id: int, queue: asyncio.Queue[str]) -> None:
        """Remove one subscriber queue from the trip event list."""

        async with self._lock:
            subscribers = self._subscribers.get(trip_id)
            if subscribers is None:
                return
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(trip_id, None)

    async def publish(self, trip_id: int, event_name: str, payload: JsonObject) -> None:
        """Publish one event to every current subscriber for the trip."""

        # Assigns stable sequence numbers to trace events so the frontend can de-duplicate frames after SSE reconnects.
        async with self._lock:
            self._prune_inactive_locked()
            # Give trace frames stable IDs for reconnect de-duplication.
            if event_name == "trace":
                self._trace_sequences[trip_id] += 1
                payload = {**payload, "sequence": self._trace_sequences[trip_id]}
            message = format_sse(event_name, payload)
            # A returning planning page needs the most recently persisted
            # stage immediately; SSE is a convenience after durable job state.
            if event_name in {"status", "planning_status"}:
                self._latest_status[trip_id] = message
            if event_name == "trace":
                self._trace_history[trip_id].append(message)
            queues = list(self._subscribers.get(trip_id, ()))
        for queue in queues:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                continue

    def _prune_inactive_locked(self) -> None:
        """Bound process-local replay data; PostgreSQL remains authoritative."""

        inactive = [
            trip_id
            for trip_id in self._latest_status
            if trip_id not in self._subscribers
        ]
        overflow = len(inactive) - MAX_TRACKED_TRIPS
        for trip_id in inactive[: max(0, overflow)]:
            self._latest_status.pop(trip_id, None)
            self._trace_history.pop(trip_id, None)
            self._trace_sequences.pop(trip_id, None)
