"""Persistent OpenAI Responses wrapper for the Planning Agent."""

from __future__ import annotations

import json
import os
import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
from langchain_core.messages import AIMessage, ToolMessage
from openai import AsyncOpenAI

from app.services.llm_usage import ensure_current_trip_can_start_model_call, record_current_llm_usage

from .config import PlanningAgentConfig


class PlanningAgentLLM:
    def __init__(self, client: AsyncOpenAI | None, config: PlanningAgentConfig) -> None:
        self.client, self.config = client, config
        self.calls = self.input_tokens = self.output_tokens = 0
        self.successful_calls = 0
        self.previous_response_id: str | None = None
        self._consumed_tool_call_ids: set[str] = set()

    @classmethod
    def from_environment(cls, config: PlanningAgentConfig | None = None) -> "PlanningAgentLLM":
        key = os.getenv("OPENAI_API_KEY")
        client = None
        if key:
            client = AsyncOpenAI(
                api_key=key, base_url=os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1"),
                max_retries=int(os.getenv("PLANNER_LLM_MAX_RETRIES", "0")),
                http_client=httpx.AsyncClient(timeout=httpx.Timeout(float(os.getenv("PLANNER_LLM_TIMEOUT_SECONDS", "75"))), trust_env=False),
            )
        return cls(client, config or PlanningAgentConfig.from_environment())

    @property
    def available(self) -> bool:
        return self.client is not None

    def reset(self) -> None:
        self.calls = self.input_tokens = self.output_tokens = 0
        self.successful_calls = 0
        self.previous_response_id = None
        self._consumed_tool_call_ids.clear()

    async def close(self) -> None:
        if self.client:
            await self.client.close()

    async def invoke(self, context: str, tools: list[dict[str, Any]], messages: list[Any]) -> AIMessage:
        if not self.client:
            raise RuntimeError("Planner Agent requires OPENAI_API_KEY.")
        if self.calls >= self.config.max_llm_calls:
            raise RuntimeError("Planner Agent LLM call limit reached.")
        outputs = []
        submitted_ids: set[str] = set()
        for message in messages:
            if isinstance(message, ToolMessage) and message.tool_call_id and message.tool_call_id not in self._consumed_tool_call_ids | submitted_ids:
                submitted_ids.add(message.tool_call_id)
                outputs.append({"type": "function_call_output", "call_id": message.tool_call_id, "output": str(message.content)})
        outputs.append({"role": "user", "content": [{"type": "input_text", "text": context}]})
        ensure_current_trip_can_start_model_call()
        request: dict[str, Any] = {"model": self.config.model, "reasoning": {"effort": self.config.reasoning_effort}, "instructions": self.config.system_prompt, "input": outputs, "tools": tools}
        if len(tools) == 1 and tools[0].get("name") == "submit_plan_proposal":
            request["tool_choice"] = {
                "type": "function",
                "name": "submit_plan_proposal",
            }
        # A bounded proposal is consumed by Python, not a tool conversation.
        # Continuing its unanswered function call makes the repair request invalid.
        if self.previous_response_id and outputs and outputs[0].get("type") == "function_call_output":
            request["previous_response_id"] = self.previous_response_id
        # The graph reserves a final deterministic window.  A slow model must
        # never consume the full planner deadline or prevent draft delivery.
        self.calls += 1
        response = await asyncio.wait_for(
            self.client.responses.create(**request),
            timeout=float(os.getenv("PLANNER_LLM_TIMEOUT_SECONDS", "75")),
        )
        self.successful_calls += 1
        self._consumed_tool_call_ids.update(submitted_ids)
        self.previous_response_id = str(getattr(response, "id", "") or "") or None
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        record_current_llm_usage("planner", self.config.model, SimpleNamespace(requests=1, input_tokens=input_tokens, output_tokens=output_tokens))
        calls = []
        for item in getattr(response, "output", []) or []:
            if getattr(item, "type", None) == "function_call":
                try:
                    args = json.loads(item.arguments)
                except (TypeError, json.JSONDecodeError):
                    args = {}
                calls.append({"name": item.name, "args": args, "id": item.call_id, "type": "tool_call"})
        return AIMessage(content=getattr(response, "output_text", ""), tool_calls=calls)

    def metrics(self) -> dict[str, Any]:
        return {"planner_llm_calls": self.calls, "planner_llm_successful_calls": self.successful_calls, "planner_input_tokens": self.input_tokens, "planner_output_tokens": self.output_tokens, "planner_llm_calls_by_model": {self.config.model: self.calls}}
