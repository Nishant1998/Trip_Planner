"""Shared agent runner helpers for the collection-phase workflow."""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Any, Awaitable, Callable, Generic, Protocol, TypeVar

from pydantic_ai import Agent
from pydantic_ai.capabilities import NativeTool
from pydantic_ai.native_tools import WebSearchTool

from app.exceptions import AgentRunError
from app.services.llm_usage import (
    read_result_usage,
    record_current_llm_usage,
    record_current_tool_usage,
    ensure_current_trip_can_start_model_call,
)
from app.services.agent_timing import record_current_agent_timing

T = TypeVar("T")
StreamTextCallback = Callable[[str], Awaitable[None]]

logger = logging.getLogger(__name__)


class AgentInitializationError(RuntimeError):
    """Raised when one agent builder fails during registry startup."""

    def __init__(self, agent_name: str, cause: Exception) -> None:
        self.agent_name = agent_name
        self.cause = cause
        message = (
            f"Failed to initialize '{agent_name}' agent: "
            f"{type(cause).__name__}: {cause}"
        )
        super().__init__(message)


class SupportsRun(Protocol[T]):
    """Minimal protocol for any agent runner used by the app."""

    async def run(self, prompt: str) -> T:
        """Run one prompt and return structured output."""

        ...


class PydanticAgentRunner(Generic[T]):
    """Thin adapter that exposes only the structured agent output."""

    def __init__(self, agent_name: str, model_name: str, agent: Agent[None, T]) -> None:
        self.agent_name = agent_name
        self.model_name = model_name
        self.agent = agent

    async def run(self, prompt: str) -> T:
        """Run the wrapped agent and return only its structured output."""

        started_at = perf_counter()
        prompt_summary = _summarize_prompt(prompt)
        try:
            ensure_current_trip_can_start_model_call()
            result = await self.agent.run(prompt)
            record_current_llm_usage(
                self.agent_name,
                self.model_name,
                read_result_usage(result),
            )
            _record_native_tool_usage(self.agent_name, result, started_at)
        except Exception as exc:
            elapsed_ms = int((perf_counter() - started_at) * 1000)
            record_current_agent_timing(
                agent_name=self.agent_name,
                model=self.model_name,
                elapsed_ms=elapsed_ms,
                success=False,
                error_type=type(exc).__name__,
            )
            logger.warning(
                "Agent run failed",
                extra={
                    "agent_name": self.agent_name,
                    "prompt_summary": prompt_summary,
                    "success": False,
                    "elapsed_ms": elapsed_ms,
                },
            )
            raise AgentRunError(self.agent_name, exc) from exc

        elapsed_ms = int((perf_counter() - started_at) * 1000)
        record_current_agent_timing(
            agent_name=self.agent_name,
            model=self.model_name,
            elapsed_ms=elapsed_ms,
            success=True,
        )
        logger.info(
            "Agent run completed",
            extra={
                "agent_name": self.agent_name,
                "prompt_summary": prompt_summary,
                "success": True,
                "elapsed_ms": elapsed_ms,
                "output_type": type(result.output).__name__,
            },
        )
        return result.output

    async def run_stream_field(
        self,
        prompt: str,
        *,
        field_name: str,
        on_text: StreamTextCallback,
    ) -> T:
        """Run the agent in streaming mode and publish one text field as it grows.

        Uses ``agent.run_stream()`` with ``stream_output()`` (pydantic-ai's
        built-in partial-validation stream) so the LLM delivers tokens to the
        caller as they are generated rather than buffering the full response.
        ``on_text`` is called with the full growing text on each update; the
        orchestrator's ``_publish_stream_text`` helper computes the delta and
        forwards only the new characters to the SSE stream.
        """

        started_at = perf_counter()
        prompt_summary = _summarize_prompt(prompt)
        final_output: T | None = None
        try:
            ensure_current_trip_can_start_model_call()
            async with self.agent.run_stream(prompt) as stream_result:
                prev_text = ""
                async for partial in stream_result.stream_output(debounce_by=None):
                    current_text = _get_string_field(partial, field_name)
                    if current_text and current_text != prev_text:
                        await on_text(current_text)
                        prev_text = current_text
                final_output = await stream_result.get_output()
                record_current_llm_usage(
                    self.agent_name,
                    self.model_name,
                    read_result_usage(stream_result),
                )
                _record_native_tool_usage(self.agent_name, stream_result, started_at)
                # Flush the definitive final text in case it differs from the
                # last partial (e.g. pydantic validators ran a last-pass fix).
                final_text = _get_string_field(final_output, field_name)
                if final_text and final_text != prev_text:
                    await on_text(final_text)

            if final_output is None:  # pragma: no cover – defensive guard
                raise RuntimeError("Streaming agent returned no output")
        except Exception as exc:
            elapsed_ms = int((perf_counter() - started_at) * 1000)
            record_current_agent_timing(
                agent_name=self.agent_name,
                model=self.model_name,
                elapsed_ms=elapsed_ms,
                success=False,
                error_type=type(exc).__name__,
            )
            logger.warning(
                "Agent streaming run failed",
                extra={
                    "agent_name": self.agent_name,
                    "prompt_summary": prompt_summary,
                    "success": False,
                    "elapsed_ms": elapsed_ms,
                },
            )
            raise AgentRunError(self.agent_name, exc) from exc

        elapsed_ms = int((perf_counter() - started_at) * 1000)
        record_current_agent_timing(
            agent_name=self.agent_name,
            model=self.model_name,
            elapsed_ms=elapsed_ms,
            success=True,
        )
        logger.info(
            "Agent streaming run completed",
            extra={
                "agent_name": self.agent_name,
                "prompt_summary": prompt_summary,
                "success": True,
                "elapsed_ms": elapsed_ms,
                "output_type": type(final_output).__name__,
                "streamed_field": field_name,
            },
        )
        return final_output


def build_structured_agent_runner(
    *,
    agent_name: str,
    model_name: str,
    output_type: type[T],
    instructions: str,
    retries: int | None = None,
    use_web_search: bool = False,
    model_settings: dict[str, Any] | None = None,
) -> SupportsRun[T]:
    """Build one structured PydanticAI runner with the shared defaults."""

    agent_kwargs: dict[str, object] = {
        "output_type": output_type,
        "instructions": instructions,
        "defer_model_check": True,
    }
    if retries is not None:
        agent_kwargs["retries"] = retries
    if model_settings is not None:
        agent_kwargs["model_settings"] = model_settings
    if use_web_search:
        agent_kwargs["capabilities"] = (NativeTool(WebSearchTool()),)

    agent = Agent(model_name, **agent_kwargs)
    return PydanticAgentRunner(agent_name, model_name, agent)


def _summarize_prompt(prompt: str, limit: int = 160) -> str:
    """Return a short single-line summary of one agent prompt."""

    normalized = " ".join(prompt.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 3]}..."


def _get_string_field(output: Any, field_name: str) -> str:
    """Read one string field from a structured agent output."""

    value = getattr(output, field_name, "")
    return value if isinstance(value, str) else ""


def _record_native_tool_usage(agent_name: str, result: Any, started_at: float) -> None:
    """Record each provider-confirmed native tool call, never inferred calls."""

    messages_value = getattr(result, "all_messages", [])
    messages = messages_value() if callable(messages_value) else messages_value
    for message in messages:
        calls_value = getattr(message, "native_tool_calls", [])
        calls = calls_value() if callable(calls_value) else calls_value
        for call, _return in calls:
            record_current_tool_usage(
                agent_name,
                "openai",
                str(getattr(call, "tool_name", "native_tool")),
                duration_ms=int((perf_counter() - started_at) * 1000),
            )
