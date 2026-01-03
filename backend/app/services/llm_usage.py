"""Request-local LLM usage collection with one batched persistence write."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from app.models.usage import LLMUsageDelta, ToolUsageDelta
from app.config import Settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _UsageScope:
    repository: Any
    user_id: int
    trip_id: int
    phase: str
    records: dict[tuple[str, str], LLMUsageDelta] = field(default_factory=dict)
    tool_records: dict[tuple[str, str, str], ToolUsageDelta] = field(
        default_factory=dict
    )


_CURRENT_SCOPE: ContextVar[_UsageScope | None] = ContextVar(
    "riya_llm_usage_scope",
    default=None,
)


class TripUsageLimitExceeded(RuntimeError):
    """Raised before a new paid model request when a configured trip cap is spent."""


def _empty_delta(agent_name: str, model: str) -> LLMUsageDelta:
    return LLMUsageDelta(agent_name=agent_name, model=model)


@contextmanager
def capture_llm_usage(
    repository: Any,
    *,
    user_id: int,
    trip_id: int,
    phase: str,
    force_new: bool = False,
) -> Iterator[None]:
    """Collect nested agent usage and persist it once when the scope ends."""

    current = _CURRENT_SCOPE.get()
    if not force_new and current is not None and current.trip_id == trip_id:
        yield
        return

    scope = _UsageScope(
        repository=repository,
        user_id=user_id,
        trip_id=trip_id,
        phase=phase,
        records={},
    )
    token = _CURRENT_SCOPE.set(scope)
    try:
        yield
    finally:
        _CURRENT_SCOPE.reset(token)
        _persist_scope(scope)


def record_current_llm_usage(agent_name: str, model: str, usage: Any) -> None:
    """Add provider-reported usage to the active trip scope, if one exists."""

    scope = _CURRENT_SCOPE.get()
    if scope is None or usage is None:
        return

    key = (agent_name, model)
    delta = scope.records.get(key) or _empty_delta(agent_name, model)
    details = getattr(usage, "details", None) or {}

    delta.requests += _non_negative_int(getattr(usage, "requests", 0))
    delta.input_tokens += _non_negative_int(getattr(usage, "input_tokens", 0))
    delta.output_tokens += _non_negative_int(getattr(usage, "output_tokens", 0))
    delta.cached_input_tokens += _non_negative_int(
        getattr(usage, "cache_read_tokens", 0)
    )
    delta.cache_write_tokens += _non_negative_int(
        getattr(usage, "cache_write_tokens", 0)
    )
    delta.reasoning_tokens += _non_negative_int(details.get("reasoning_tokens", 0))
    scope.records[key] = delta


def record_current_tool_usage(
    agent_name: str,
    provider: str,
    tool_name: str,
    *,
    calls: int = 1,
    failed_calls: int = 0,
    duration_ms: int = 0,
) -> None:
    """Add actual external-tool use to the active trip scope, if any."""

    scope = _CURRENT_SCOPE.get()
    if scope is None:
        return
    key = (agent_name, provider, tool_name)
    delta = scope.tool_records.get(key) or ToolUsageDelta(
        agent_name=agent_name, provider=provider, tool_name=tool_name
    )
    delta.calls += _non_negative_int(calls)
    delta.failed_calls += _non_negative_int(failed_calls)
    delta.duration_ms += _non_negative_int(duration_ms)
    scope.tool_records[key] = delta


def ensure_current_trip_can_start_model_call() -> None:
    """Stop before the next paid request only when an operator enabled a cap.

    A zero value is intentionally observe-only while realistic thresholds are
    measured.  The check includes the current in-memory batch so rapid turns
    cannot bypass a limit before it is written to PostgreSQL.
    """

    scope = _CURRENT_SCOPE.get()
    if scope is None:
        return
    settings = Settings.from_env()
    token_limit = settings.max_llm_tokens_per_trip
    tool_limit = settings.max_tool_calls_per_trip
    if not token_limit and not tool_limit:
        return
    summary = scope.repository.get_trip_usage(scope.trip_id)
    batch_tokens = sum(
        item.input_tokens + item.output_tokens for item in scope.records.values()
    )
    batch_tools = sum(item.calls for item in scope.tool_records.values())
    if token_limit and summary.llm.total_tokens + batch_tokens >= token_limit:
        raise TripUsageLimitExceeded(
            "This trip has reached its model-use limit. Please retry after the limit is adjusted."
        )
    if tool_limit and summary.tools.calls + batch_tools >= tool_limit:
        raise TripUsageLimitExceeded(
            "This trip has reached its research-tool limit. Please retry after the limit is adjusted."
        )


def read_result_usage(result: Any) -> Any:
    """Read usage from the supported PydanticAI property interface."""

    return getattr(result, "usage", None)


def _persist_scope(scope: _UsageScope) -> None:
    records = [record for record in scope.records.values() if record.requests > 0]
    recorder = getattr(scope.repository, "record_llm_usage", None)
    if records and callable(recorder):
        try:
            recorder(
                user_id=scope.user_id,
                trip_id=scope.trip_id,
                phase=scope.phase,
                records=records,
            )
        except Exception:  # Usage accounting must never break the travel conversation.
            logger.exception(
                "Failed to persist LLM usage",
                extra={"user_id": scope.user_id, "trip_id": scope.trip_id},
            )

    tool_records = [record for record in scope.tool_records.values() if record.calls]
    tool_recorder = getattr(scope.repository, "record_tool_usage", None)
    if not tool_records or not callable(tool_recorder):
        return
    try:
        tool_recorder(
            user_id=scope.user_id,
            trip_id=scope.trip_id,
            phase=scope.phase,
            records=tool_records,
        )
    except Exception:
        logger.exception(
            "Failed to persist tool usage",
            extra={"user_id": scope.user_id, "trip_id": scope.trip_id},
        )


def _non_negative_int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0
