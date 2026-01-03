"""Durable, privacy-conscious timing records for collection agents."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _TimingScope:
    user_id: int
    trip_id: int
    phase: str


_CURRENT_SCOPE: ContextVar[_TimingScope | None] = ContextVar(
    "collection_agent_timing_scope", default=None
)
_WRITE_LOCK = Lock()
_TIMING_ROOT = Path(__file__).resolve().parents[1] / "dev_runs" / "collection"


@contextmanager
def capture_agent_timing(*, user_id: int, trip_id: int, phase: str) -> Iterator[None]:
    """Attach one user/trip context to all nested collection-agent calls."""

    current = _CURRENT_SCOPE.get()
    if current is not None and current.trip_id == trip_id:
        yield
        return

    token = _CURRENT_SCOPE.set(
        _TimingScope(user_id=user_id, trip_id=trip_id, phase=phase)
    )
    try:
        yield
    finally:
        _CURRENT_SCOPE.reset(token)


def record_current_agent_timing(
    *,
    agent_name: str,
    model: str,
    elapsed_ms: int,
    success: bool,
    error_type: str | None = None,
) -> None:
    """Append one agent attempt to that trip's local JSONL timing log.

    This intentionally excludes prompts, message contents, credentials, and
    model outputs. Timing diagnostics must never interrupt a traveller's turn.
    """

    scope = _CURRENT_SCOPE.get()
    if scope is None:
        return

    record = {
        "recorded_at": datetime.now(UTC).isoformat(),
        **asdict(scope),
        "agent_name": agent_name,
        "model": model,
        "elapsed_ms": max(int(elapsed_ms), 0),
        "success": success,
        "error_type": error_type,
    }
    target = _TIMING_ROOT / f"trip-{scope.trip_id}" / "agent-timings.jsonl"
    try:
        with _WRITE_LOCK:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        logger.exception(
            "Failed to persist collection-agent timing",
            extra={"trip_id": scope.trip_id, "user_id": scope.user_id},
        )


def record_trip_turn_timing(
    *,
    user_id: int,
    trip_id: int,
    elapsed_ms: int,
    success: bool,
    error_type: str | None = None,
) -> None:
    """Append one end-to-end collection-turn duration beside its agent logs."""

    record = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "user_id": user_id,
        "trip_id": trip_id,
        "phase": "collection",
        "elapsed_ms": max(int(elapsed_ms), 0),
        "success": success,
        "error_type": error_type,
    }
    target = _TIMING_ROOT / f"trip-{trip_id}" / "turn-timings.jsonl"
    try:
        with _WRITE_LOCK:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        logger.exception(
            "Failed to persist collection-turn timing",
            extra={"trip_id": trip_id, "user_id": user_id},
        )
