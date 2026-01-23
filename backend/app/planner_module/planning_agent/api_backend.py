"""Responses API adapter for the shared planner request/reply contract."""
import json
from copy import deepcopy
from types import SimpleNamespace

from openai import APIError

from app.services.llm_usage import TripUsageLimitExceeded, ensure_current_trip_can_start_model_call, record_current_llm_usage
from .resources import ResourceLimit
from .contracts import UpdateEnvelope
from .protocol import ModelReply
from .wire import WireUpdate, WireReview
from .agent_outputs import PresentationResult
from .stages import AgentRole


class PlannerAPIBackend:
    def __init__(self, client, config):
        self.client, self.config = client, config
        self.previous_response_id = None
        self.pending_call_ids = []
        self.active_agent = "legacy"
        self.agent_histories = {}
        self.requests = self.input_tokens = self.output_tokens = 0
        self.max_output_tokens = getattr(config, "max_output_tokens", 32768)
        if type(self.max_output_tokens) is not int or self.max_output_tokens < 1:
            raise ValueError("Planner output token limit must be a positive integer")

    def snapshot(self):
        return {"backend": "api", "previous_response_id": self.previous_response_id,
                "pending_call_ids": list(self.pending_call_ids), "requests": self.requests,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "max_output_tokens": self.max_output_tokens,
                "active_agent": self.active_agent, "agent_histories": deepcopy(self.agent_histories)}

    def restore(self, state):
        if state.get("backend") != "api":
            raise ValueError("Cannot restore another model backend's state")
        self.previous_response_id = state["previous_response_id"]
        self.pending_call_ids = list(state["pending_call_ids"])
        self.requests, self.input_tokens, self.output_tokens = (state[key] for key in ("requests", "input_tokens", "output_tokens"))
        limit = state.get("max_output_tokens", self.max_output_tokens)
        if type(limit) is not int or limit < 1:
            raise ValueError("Invalid saved planner output token limit")
        self.max_output_tokens = limit
        self.active_agent = state.get("active_agent", "legacy")
        self.agent_histories = deepcopy(state.get("agent_histories", {}))

    def _select_agent(self, request):
        agent = request.stage.agent.value if request.stage else "legacy"
        if agent == self.active_agent:
            return
        if self.pending_call_ids:
            raise ValueError("Cannot switch agents with unacknowledged tool calls")
        self.agent_histories[self.active_agent] = self.previous_response_id
        self.previous_response_id = self.agent_histories.get(agent)
        self.active_agent = agent

    async def invoke(self, request):
        if self.client is None:
            raise ValueError("Planner model credentials unavailable")
        self._select_agent(request)
        observations = {item["call_id"]: item["result"] for item in request.observations}
        inputs = [{"type": "function_call_output", "call_id": call_id,
                   "output": json.dumps(observations.get(call_id, {
                       "status": "rejected", "reason": "response_not_dispatched_by_application"}), allow_nan=False)}
                  for call_id in self.pending_call_ids]
        inputs.append({"role": "user", "content": json.dumps({
            "run_id": request.run_id, "turn_id": request.turn_id,
            "request_fingerprint": request.request_fingerprint,
            "input_fingerprint": request.input_fingerprint,
            "plan_version": request.plan_version, "evidence_revision": request.evidence_revision,
            "stage": request.stage.model_dump(mode="json") if request.stage else None,
            "context": request.context, "observations": request.observations,
            "output_instructions": ("Return the JSON result matching result_schema. Use tools only when exposed; never mix calls and text."
                                    if request.stage else "Call utilities OR return one JSON UpdateEnvelope matching update_schema; do not mix them.")
        }, allow_nan=False)})
        tools = deepcopy(request.tool_schemas)
        kwargs = dict(model=self.config.model, reasoning={"effort": self.config.reasoning_effort},
                      instructions=request.instructions, input=inputs, tools=tools,
                      max_output_tokens=self.max_output_tokens)
        if self.previous_response_id:
            kwargs["previous_response_id"] = self.previous_response_id
        if request.stage:
            kwargs["text"] = {"format": {"type": "json_schema", "name": request.context["result_kind"],
                "strict": True, "schema": deepcopy(request.context["result_schema"])}}
        elif request.context.get("update_format") == "entries_v1":
            kwargs["text"] = {"format": {"type": "json_schema", "name": "planner_update",
                "strict": True, "schema": deepcopy(request.context["update_schema"])}}
        try:
            ensure_current_trip_can_start_model_call()
        except TripUsageLimitExceeded as exc:
            raise ResourceLimit("user_llm_quota") from exc
        self.requests += 1
        try:
            response = await self.client.with_options(max_retries=0).responses.create(**kwargs)
        except APIError as exc:
            raise ValueError("Planner model provider request failed") from exc
        # Successful protocol progression acknowledges the old outputs even when
        # the new semantic answer is malformed. Failed requests never do.
        self.previous_response_id = response.id
        self.pending_call_ids = [item.call_id for item in response.output if item.type == "function_call"]
        usage = getattr(response, "usage", None)
        incoming, outgoing = getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0
        self.input_tokens += incoming
        self.output_tokens += outgoing
        record_current_llm_usage("planner", self.config.model,
            SimpleNamespace(requests=1, input_tokens=incoming, output_tokens=outgoing))
        if getattr(response, "status", None) != "completed":
            # Keep usage and continuation IDs, but never execute partial output.
            # The next bounded turn supplies denials for these unexecuted calls.
            raise ValueError("Planner model response not completed")
        if any(getattr(item, "status", None) not in {None, "completed"}
               for item in response.output if item.type in {"function_call", "message"}):
            raise ValueError("Incomplete planner output item")
        calls = [{"id": item.call_id, "name": item.name, "arguments": json.loads(item.arguments)}
                 for item in response.output if item.type == "function_call"]
        text = response.output_text.strip()
        if calls and text:
            raise ValueError("Mixed utility and textual planner response")
        update = review = presentation = None
        if not calls:
            if request.stage and request.stage.agent == AgentRole.REVIEW:
                review = WireReview.model_validate_json(text).to_internal()
            elif request.stage and request.stage.agent == AgentRole.PRESENTATION:
                presentation = PresentationResult.model_validate_json(text)
            else:
                update = (WireUpdate.model_validate_json(text).to_internal()
                      if request.context.get("update_format") == "entries_v1"
                      else UpdateEnvelope.model_validate_json(text))
        reply = ModelReply(run_id=request.run_id, turn_id=request.turn_id,
            request_fingerprint=request.request_fingerprint, stage=request.stage,
            calls=calls, update=update, review=review, presentation=presentation)
        reply.match(request)
        return reply
