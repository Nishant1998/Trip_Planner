"""OpenAI Responses wrapper that emits LangChain-compatible tool calls."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any

import httpx
from langchain_core.messages import AIMessage, ToolMessage
from openai import AsyncOpenAI

from app.services.llm_usage import (
    ensure_current_trip_can_start_model_call,
    record_current_llm_usage,
)

from .config import ResearchAgentConfig


class ResearchAgentLLM:
    def __init__(self, client: AsyncOpenAI, config: ResearchAgentConfig) -> None:
        self.client, self.config = client, config
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.previous_response_id: str | None = None
        self._consumed_tool_call_ids: set[str] = set()

    @classmethod
    def from_environment(cls, config: ResearchAgentConfig | None = None) -> ResearchAgentLLM:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("Research Agent V2 requires OPENAI_API_KEY.")
        client = AsyncOpenAI(
            api_key=key,
            base_url=os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1"),
            max_retries=int(os.getenv("RESEARCH_LLM_MAX_RETRIES", "2")),
            http_client=httpx.AsyncClient(
                timeout=httpx.Timeout(float(os.getenv("LLM_HTTP_TIMEOUT_SECONDS", "60"))),
                trust_env=False,
            ),
        )
        return cls(client, config or ResearchAgentConfig.from_environment())

    async def close(self) -> None:
        await self.client.close()

    def reset(self) -> None:
        """Clear one run's Responses conversation while retaining configured clients."""
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.previous_response_id = None
        self._consumed_tool_call_ids.clear()

    def _tool_outputs(self, messages: list[Any]) -> list[dict[str, str]]:
        outputs: list[dict[str, str]] = []
        for message in messages:
            if not isinstance(message, ToolMessage):
                continue
            call_id = str(message.tool_call_id or "")
            if not call_id or call_id in self._consumed_tool_call_ids:
                continue
            self._consumed_tool_call_ids.add(call_id)
            outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": str(message.content),
                }
            )
        return outputs

    async def invoke(
        self,
        runtime_context: str,
        tools: list[dict[str, Any]],
        messages: list[Any] | None = None,
    ) -> AIMessage:
        if self.calls >= self.config.max_llm_calls:
            raise RuntimeError("Research Agent V2 LLM call limit reached.")
        ensure_current_trip_can_start_model_call()
        inputs = self._tool_outputs(messages or [])
        inputs.append(
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": runtime_context},
                ],
            }
        )
        request: dict[str, Any] = {
            "model": self.config.model,
            "reasoning": {"effort": self.config.reasoning_effort},
            "instructions": self.config.system_prompt,
            "input": inputs,
            "tools": tools,
        }
        if self.previous_response_id:
            request["previous_response_id"] = self.previous_response_id
        response = await self.client.responses.create(**request)
        self.calls += 1
        self.previous_response_id = str(getattr(response, "id", "") or "") or None
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        record_current_llm_usage("research", self.config.model, SimpleNamespace(
            requests=1, input_tokens=input_tokens, output_tokens=output_tokens,
        ))
        calls = []
        for item in getattr(response, "output", []) or []:
            if getattr(item, "type", None) != "function_call":
                continue
            try:
                arguments = json.loads(item.arguments)
            except (TypeError, json.JSONDecodeError):
                arguments = {}
            calls.append({"name": item.name, "args": arguments, "id": item.call_id, "type": "tool_call"})
        return AIMessage(
            content=getattr(response, "output_text", ""),
            tool_calls=calls,
            additional_kwargs={"response_id": self.previous_response_id},
        )

    def metrics(self) -> dict[str, Any]:
        return {
            "llm_calls": self.calls,
            "llm_input_tokens": self.input_tokens,
            "llm_output_tokens": self.output_tokens,
            "llm_calls_by_model": {self.config.model: self.calls},
            "llm_input_tokens_by_model": {self.config.model: self.input_tokens},
            "llm_output_tokens_by_model": {self.config.model: self.output_tokens},
        }
