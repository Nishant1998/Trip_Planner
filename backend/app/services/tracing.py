"""Optional LangSmith spans containing operational metadata only.

No arguments, return values, prompts, profiles or exception messages are sent.
The SDK batches exports on a worker thread; export failures never affect a trip.
"""

from __future__ import annotations

import inspect
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import wraps
from time import perf_counter
from uuid import uuid4

from app.services.logging import request_id_context

logger = logging.getLogger(__name__)
_client = None
_project = "TripAi"
_current: ContextVar[Span | None] = ContextVar("tripai_span", default=None)
_FIELDS = {
    "request_id", "trip_id", "job_id", "entry_point", "agent_name", "role",
    "model_name", "tool_name", "status", "error_type", "duration_ms",
    "input_tokens", "output_tokens", "requests", "stage", "tool_status",
    "ls_provider", "ls_model_name", "native_tool_calls",
}


def _metadata(values):
    clean = {key: value for key, value in values.items()
             if key in _FIELDS and isinstance(value, (str, int, float, bool))
             and (not isinstance(value, str) or len(value) <= 120)}
    usage = values.get("usage_metadata")
    if isinstance(usage, dict):
        clean["usage_metadata"] = {key: value for key, value in usage.items()
                                   if key in {"input_tokens", "output_tokens", "total_tokens"}
                                   and isinstance(value, int) and value >= 0}
    return clean


def configure_tracing() -> None:
    """Enable explicit spans only, preventing automatic graph-state uploads."""
    global _client, _project
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    close_tracing()
    enabled = os.getenv("TRIPAI_TRACING_ENABLED", "false").lower() == "true"
    if not enabled:
        return
    key = os.getenv("LANGSMITH_API_KEY", "")
    if not key:
        logger.warning("tracing_not_configured")
        return
    try:
        import requests
        from langsmith import Client
        from urllib3.util import Retry

        session = requests.Session()
        session.trust_env = False
        _project = os.getenv("LANGSMITH_PROJECT", "TripAi")
        _client = Client(
            api_key=key,
            api_url=os.getenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"),
            workspace_id=os.getenv("LANGSMITH_WORKSPACE_ID") or None,
            session=session,
            timeout_ms=2000,
            retry_config=Retry(total=0),
            auto_batch_tracing=True,
            hide_inputs=True,
            hide_outputs=True,
            hide_metadata=_metadata,
            omit_traced_runtime_info=True,
            tracing_error_callback=lambda error: logger.warning(
                "trace_export_failed", extra={"error_type": type(error).__name__}
            ),
        )
        logger.info("tracing_enabled")
    except Exception as error:
        _client = None
        logger.warning("tracing_unavailable", extra={"error_type": type(error).__name__})


def close_tracing() -> None:
    global _client
    client, _client = _client, None
    if client is not None:
        try:
            client.close(timeout=3)
        except Exception:
            logger.warning("trace_shutdown_failed")


@dataclass
class Span:
    id: str
    trace_id: str
    dotted_order: str
    metadata: dict = field(default_factory=dict)
    error: str | None = None


def trace_metadata(**values) -> None:
    span = _current.get()
    if span is not None:
        span.metadata.update(_metadata(values))


def trace_failure(error_type: str) -> None:
    """Mark failures that the business code handles rather than raises."""
    span = _current.get()
    if span is not None:
        span.error = error_type if error_type.isidentifier() else "OperationFailed"


@contextmanager
def trace_span(name: str, *, run_type="chain", root=False, **metadata):
    client = _client
    if client is None:
        yield
        return
    parent = None if root else _current.get()
    now = datetime.now(UTC)
    run_id = str(uuid4())
    order = now.strftime("%Y%m%dT%H%M%S%fZ") + run_id
    span = Span(run_id, parent.trace_id if parent else run_id,
                f"{parent.dotted_order}.{order}" if parent else order,
                dict(parent.metadata) if parent else {})
    # Usage and outcomes belong to this operation, not its parent.
    for key in ("input_tokens", "output_tokens", "usage_metadata", "requests", "duration_ms", "status", "error_type"):
        span.metadata.pop(key, None)
    span.metadata.update(_metadata(metadata))
    if run_type == "llm" and span.metadata.get("model_name"):
        span.metadata.update(ls_provider="openai",
                             ls_model_name=span.metadata["model_name"].removeprefix("openai:"))
    span.metadata["request_id"] = request_id_context.get()
    try:
        client.create_run(name, inputs={}, run_type=run_type, id=run_id,
                          trace_id=span.trace_id, dotted_order=span.dotted_order,
                          parent_run_id=parent.id if parent else None,
                          start_time=now, project_name=_project,
                          extra={"metadata": span.metadata}, tags=["tripai", "private-metadata"])
    except Exception:
        logger.warning("trace_start_failed")
        yield
        return
    token = _current.set(span)
    started = perf_counter()
    try:
        yield
    except BaseException as error:
        trace_failure(type(error).__name__)
        raise
    finally:
        _current.reset(token)
        span.metadata.update(duration_ms=round((perf_counter() - started) * 1000),
                             status="failed" if span.error else "completed")
        if span.error:
            span.metadata["error_type"] = span.error
        try:
            usage = {}
            if run_type == "llm" and "input_tokens" in span.metadata:
                usage = {key: span.metadata.get(key, 0) for key in ("input_tokens", "output_tokens")}
                usage["total_tokens"] = sum(usage.values())
                span.metadata["usage_metadata"] = usage
            client.update_run(run_id, end_time=datetime.now(UTC), outputs={},
                              error=span.error, trace_id=span.trace_id,
                              dotted_order=span.dotted_order,
                              parent_run_id=parent.id if parent else None,
                              extra={"metadata": span.metadata})
        except Exception:
            logger.warning("trace_finish_failed")
        logger.info("operation_finished", extra={**span.metadata, "trace_id": run_id,
                                                  "operation": name})


def observed(name, *, run_type="chain", root=False, fields=(), attributes=(),
             result_status=False, **metadata):
    """Wrap an async operation without serializing its arguments or result."""
    def decorate(function):
        signature = inspect.signature(function)

        @wraps(function)
        async def wrapped(*args, **kwargs):
            if _client is None:
                return await function(*args, **kwargs)
            bound = signature.bind(*args, **kwargs).arguments
            selected = {key: bound[key] for key in fields if key in bound}
            instance = bound.get("self")
            selected.update({key: getattr(instance, key) for key in attributes})
            label = selected.get("role") or selected.get("agent_name")
            operation = f"{name}.{label}" if isinstance(label, str) and label.isidentifier() else name
            with trace_span(operation, run_type=run_type, root=root, **metadata, **selected):
                result = await function(*args, **kwargs)
                if result_status:
                    status = str(result.status)
                    trace_metadata(tool_status=status)
                    if status in {"error", "unavailable"}:
                        trace_failure("ToolUnavailable" if status == "unavailable" else "ToolFailed")
                return result
        return wrapped
    return decorate
