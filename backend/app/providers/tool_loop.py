"""One small Responses API model-tool-model loop shared by both agents."""

from __future__ import annotations

import json
from asyncio import sleep
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Protocol, cast

import httpx
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel, ValidationError

from app.models.agent import AgentUsage, ToolCallAudit
from app.models.progress import PlanningStage
from app.services.progress import ProgressReporter
from app.services.tracing import observed, trace_failure, trace_metadata

ToolHandler = Callable[[BaseModel], Awaitable[BaseModel | dict[str, Any] | list[Any]]]


class AgentToolInputError(ValueError):
    """A safe, actionable tool-input error that the calling agent may see."""


class ModelOutputIncomplete(RuntimeError):
    """A schema retry with the same output budget cannot repair truncated JSON."""


@dataclass(frozen=True, slots=True)
class AgentTool:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: ToolHandler
    stage: PlanningStage
    allow_repeated_calls: bool = False

    def api_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.input_model.model_json_schema(),
            "strict": False,
        }


@dataclass(slots=True)
class AgentRun:
    output: BaseModel
    usage: AgentUsage = field(default_factory=AgentUsage)


class ToolCallingModel(Protocol):
    async def run(
        self,
        *,
        agent_name: str,
        instructions: str,
        input_payload: dict[str, Any],
        tools: list[AgentTool],
        output_model: type[BaseModel],
        max_tool_calls: int,
        progress: ProgressReporter,
        turn_stage: Callable[[], PlanningStage],
    ) -> AgentRun: ...


class OpenAIResponsesToolModel:
    """Expose typed Python tools to one model with a strict call budget."""

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float,
        *,
        max_model_requests: int | None = None,
        max_output_tokens: int | None = None,
        request_reserves: dict[str, int] | None = None,
    ) -> None:
        if any(value is not None and value < 1 for value in (max_model_requests, max_output_tokens)):
            raise ValueError("Model request and output limits must be positive")
        self.model_name = model
        self.max_model_requests = max_model_requests
        self.max_output_tokens = max_output_tokens
        self.requests_started = 0
        self.request_reserves = dict(request_reserves or {})
        if any(value < 0 for value in self.request_reserves.values()):
            raise ValueError("Model request reserves must be nonnegative")
        # This workstation has an unavailable system proxy. Planning must use
        # the known-good direct HTTPS route, just like the web-search provider.
        self._http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=False,
        )
        self._client = AsyncOpenAI(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=0,
            http_client=self._http_client,
        )

    async def run(
        self,
        *,
        agent_name: str,
        instructions: str,
        input_payload: dict[str, Any],
        tools: list[AgentTool],
        output_model: type[BaseModel],
        max_tool_calls: int,
        progress: ProgressReporter,
        turn_stage: Callable[[], PlanningStage],
    ) -> AgentRun:
        tool_map = {tool.name: tool for tool in tools}
        tool_schemas = [tool.api_schema() for tool in tools]
        next_input: Any = json.dumps(input_payload, ensure_ascii=False, default=str)
        previous_response_id: str | None = None
        usage = AgentUsage()
        seen_calls: set[tuple[str, str]] = set()

        while True:
            request: dict[str, Any] = {
                "model": self.model_name,
                "instructions": instructions,
                "input": next_input,
                "previous_response_id": previous_response_id,
                "text_format": output_model,
                # itinerary planning already keeps factual feasibility in deterministic
                # Python.  Ranking benefits from a little reasoning, while a
                # read-only critic should be fast and decisive.
                "reasoning": {"effort": _reasoning_effort(agent_name)},
            }
            if tool_schemas:
                request["tools"] = cast(Any, tool_schemas)
                request["parallel_tool_calls"] = False
            response = await self._parse_with_rate_limit_retry(
                progress=progress,
                stage=turn_stage(),
                request_reserve=self.request_reserves.get(agent_name, 0),
                **request,
            )
            _add_usage(usage, getattr(response, "usage", None))
            calls = [item for item in response.output if item.type == "function_call"]
            if not calls:
                parsed = response.output_parsed
                if parsed is None:
                    raise RuntimeError(f"{agent_name} returned no structured result.")
                return AgentRun(output=parsed, usage=usage)

            if len(usage.tool_calls) + len(calls) > max_tool_calls:
                raise RuntimeError(f"{agent_name} exceeded its tool-call limit.")

            outputs: list[dict[str, str]] = []
            for call in calls:
                arguments = str(getattr(call, "arguments", "{}"))
                signature = (str(call.name), arguments)
                tool = tool_map.get(str(call.name))
                repeat_allowed = bool(tool and tool.allow_repeated_calls)
                if signature in seen_calls and not repeat_allowed:
                    output, audit = _duplicate_call(agent_name, call, arguments)
                else:
                    seen_calls.add(signature)
                    output, audit = await _invoke_tool(
                        agent_name,
                        call,
                        arguments,
                        tool_map,
                        progress,
                    )
                usage.tool_calls.append(audit)
                await progress.record_tool_call(audit)
                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(call.call_id),
                        "output": output,
                    }
                )
            previous_response_id = response.id
            next_input = outputs

    async def close(self) -> None:
        await self._client.close()

    @observed("planning.model", run_type="llm", attributes=("model_name",))
    async def _parse_with_rate_limit_retry(
        self,
        *,
        progress: ProgressReporter,
        stage: PlanningStage,
        request_reserve: int = 0,
        **request: Any,
    ) -> Any:
        """Retry temporary provider failures without abandoning a trip."""
        # A slow model must not turn one planning job into several minutes of
        # invisible retries.  Provider-specific fallbacks create an adjusted
        # itinerary when a bounded attempt cannot complete.
        for attempt in range(2):
            if (self.max_model_requests is not None
                    and self.requests_started >= self.max_model_requests - request_reserve):
                raise RuntimeError("Planning model request limit reached")
            if self.max_output_tokens is not None:
                request["max_output_tokens"] = self.max_output_tokens
            try:
                async with progress.track(stage):
                    self.requests_started += 1
                    response = await self._client.responses.parse(**request)
                    usage = getattr(response, "usage", None)
                    if usage is not None:
                        trace_metadata(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)
                    return response
            except ValidationError as error:
                if any(item["type"] == "json_invalid" and "EOF" in item["msg"]
                       for item in error.errors(include_input=False, include_url=False)):
                    raise ModelOutputIncomplete(
                        "Planning output was truncated; review the response-token allowance."
                    ) from None
                raise
            except (
                APIConnectionError,
                APITimeoutError,
                InternalServerError,
                RateLimitError,
            ) as error:
                if attempt == 1 or isinstance(error, APITimeoutError):
                    raise
                await progress.retrying()
                delay_seconds = (
                    10 * (attempt + 1)
                    if isinstance(error, RateLimitError)
                    else 2**attempt
                )
                await sleep(delay_seconds)
        raise RuntimeError(
            "Transient provider retry loop completed without a response."
        )


def _reasoning_effort(agent_name: str) -> str:
    return {
        "research": "low",
        "planner": "low",
        "reviewer": "none",
    }.get(agent_name, "low")


@observed("planning.tool", run_type="tool", fields=("agent_name",))
async def _invoke_tool(
    agent_name: str,
    call: Any,
    raw_arguments: str,
    tools: dict[str, AgentTool],
    progress: ProgressReporter,
) -> tuple[str, ToolCallAudit]:
    started_at = datetime.now(UTC)
    started = perf_counter()
    name = str(call.name)
    trace_metadata(tool_name=name if name in tools else "unknown_tool")
    raw: dict[str, object] = {}
    status = "ok"
    try:
        tool = tools[name]
        decoded = json.loads(raw_arguments)
        raw = decoded if isinstance(decoded, dict) else {}
        arguments = tool.input_model.model_validate(raw)
        async with progress.track(tool.stage):
            result = await tool.handler(arguments)
        output = _json_result(result)
    except (KeyError, json.JSONDecodeError, ValidationError) as exc:
        trace_failure(type(exc).__name__)
        status = "invalid_arguments"
        output = _json_error("invalid_arguments", str(exc))
    except AgentToolInputError as exc:
        trace_failure(type(exc).__name__)
        status = "invalid_arguments"
        output = _json_error("tool_input_rejected", str(exc)[:500])
    except Exception as exc:
        trace_failure(type(exc).__name__)
        status = "failed"
        output = _json_error("tool_failed", type(exc).__name__)
    return output, ToolCallAudit(
        agent=cast(Any, agent_name),
        call_id=str(call.call_id),
        tool_name=name,
        arguments=raw,
        status=cast(Any, status),
        started_at=started_at,
        duration_ms=round((perf_counter() - started) * 1000),
        result_summary=_summary(output),
    )


def _duplicate_call(
    agent_name: str,
    call: Any,
    raw_arguments: str,
) -> tuple[str, ToolCallAudit]:
    output = _json_error(
        "duplicate_call", "Use the previous result or change arguments."
    )
    try:
        decoded = json.loads(raw_arguments)
        raw = decoded if isinstance(decoded, dict) else {}
    except json.JSONDecodeError:
        raw = {}
    return output, ToolCallAudit(
        agent=cast(Any, agent_name),
        call_id=str(call.call_id),
        tool_name=str(call.name),
        arguments=raw,
        status="duplicate",
        started_at=datetime.now(UTC),
        duration_ms=0,
        result_summary=_summary(output),
    )


def _json_result(result: BaseModel | dict[str, Any] | list[Any]) -> str:
    payload = (
        result.model_dump(mode="json") if isinstance(result, BaseModel) else result
    )
    return json.dumps(
        {"status": "ok", "data": payload}, ensure_ascii=False, default=str
    )


def _json_error(code: str, message: str) -> str:
    return json.dumps({"status": "error", "code": code, "message": message})


def _summary(output: str) -> str:
    return " ".join(output.split())[:240]


def _add_usage(usage: AgentUsage, raw: Any) -> None:
    if raw is None:
        return
    usage.input_tokens += int(getattr(raw, "input_tokens", 0) or 0)
    usage.output_tokens += int(getattr(raw, "output_tokens", 0) or 0)
