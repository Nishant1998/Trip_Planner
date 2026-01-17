"""Serializable model exchange and utility ledger shared by planner backends."""
from typing import Any, Literal
from copy import deepcopy

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import UpdateEnvelope, fingerprint
from .agent_outputs import PresentationResult, ReviewResult
from .stages import AgentRole, AgentStage

UtilityName = Literal["calculate", "get_routes", "optimize_stop_order",
                      "check_timeline", "web_search", "read_webpage"]


def strict_object_schema(schema: dict) -> dict:
    """Require explicit fields without allowing arbitrary model-selected keys."""
    result = deepcopy(schema)

    def convert(node):
        node.pop("default", None)
        if node.get("type") == "object":
            if node.get("additionalProperties") not in (None, False):
                raise ValueError("Variable-key maps require a typed wire representation")
            properties = node.get("properties", {})
            node["additionalProperties"] = False
            node["required"] = list(properties)
            for child in properties.values():
                convert(child)
        for child in node.get("$defs", {}).values():
            convert(child)
        for child in node.get("anyOf", []):
            convert(child)
        if "items" in node:
            convert(node["items"])

    if result.get("type") != "object":
        raise ValueError("Structured planner schema requires an object root")
    convert(result)
    return result


class UtilityCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    name: UtilityName
    arguments: dict[str, Any]


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    owner_scope: str
    turn_id: str
    input_fingerprint: str
    plan_version: int = Field(ge=0)
    evidence_revision: int = Field(ge=0)
    instructions: str
    context: dict[str, Any]
    tool_schemas: list[dict[str, Any]]
    observations: list[dict[str, Any]] = Field(default_factory=list)
    stage: AgentStage | None = None

    @model_validator(mode="after")
    def stage_capabilities(self):
        if self.stage and not self.stage.allows_tools and self.tool_schemas:
            raise ValueError("Presentation cannot receive executable tools")
        return self

    @property
    def request_fingerprint(self) -> str:
        value = self.model_dump(mode="json")
        if self.stage is None:
            value.pop("stage")  # Preserve fingerprints of historical pending requests.
        return fingerprint(value)


class ModelReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    turn_id: str
    request_fingerprint: str
    calls: list[UtilityCall] = Field(default_factory=list, max_length=32)
    update: UpdateEnvelope | None = None
    stage: AgentStage | None = None
    review: ReviewResult | None = None
    presentation: PresentationResult | None = None

    @model_validator(mode="after")
    def one_response_kind(self):
        if sum((bool(self.calls), self.update is not None, self.review is not None,
                self.presentation is not None)) != 1:
            raise ValueError("Reply requires exactly one response kind")
        if self.stage is None:
            if self.review is not None or self.presentation is not None:
                raise ValueError("Agent identity required for review/presentation")
        elif self.stage.agent == AgentRole.PLANNING:
            if self.review is not None or self.presentation is not None:
                raise ValueError("Planning agent cannot return another agent's output")
        elif self.stage.agent == AgentRole.REVIEW:
            if self.update is not None or self.presentation is not None:
                raise ValueError("Review-and-Repair must return its typed result or tool calls")
        elif self.presentation is None:
            raise ValueError("Presentation agent cannot call tools or change the plan")
        if len({call.id for call in self.calls}) != len(self.calls):
            raise ValueError("Duplicate tool call IDs")
        return self

    def match(self, request: ModelRequest):
        if self.stage != request.stage:
            raise ValueError("Reply agent/task does not match pending request")
        if (self.run_id, self.turn_id, self.request_fingerprint) != (
                request.run_id, request.turn_id, request.request_fingerprint):
            raise ValueError("Reply does not match pending request")
        update = self.update or (self.review.corrected_candidate if self.review else None)
        if update and (
            update.input_fingerprint != request.input_fingerprint
            or update.base_plan_version != request.plan_version
            or update.expected_evidence_revision != request.evidence_revision
        ):
            raise ValueError("Stale typed update")

    @property
    def reply_fingerprint(self):
        value = self.model_dump(mode="json")
        if self.stage is None:
            for field in ("stage", "review", "presentation"):
                value.pop(field)
        return fingerprint(value)


class CallRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    call: UtilityCall
    status: Literal["pending", "started", "completed"] = "pending"
    result: dict[str, Any] | None = None


class ExchangeLedger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    owner_scope: str
    pending: ModelRequest | None = None
    accepted_replies: dict[str, str] = Field(default_factory=dict)
    calls: dict[str, CallRecord] = Field(default_factory=dict)

    def accept(self, reply: ModelReply):
        digest = reply.reply_fingerprint
        previous = self.accepted_replies.get(reply.turn_id)
        if reply.run_id != self.run_id:
            raise ValueError("Wrong run")
        if previous:
            if previous != digest:
                raise ValueError("Consumed turn cannot change")
            return False
        if self.pending is None or self.pending.owner_scope != self.owner_scope:
            raise ValueError("No authorized pending request")
        reply.match(self.pending)
        if any(call.id in self.calls for call in reply.calls):
            raise ValueError("Tool call ID reused across turns")
        # Validate the whole exchange before mutation.
        self.calls.update({call.id: CallRecord(call=call.model_copy(deep=True)) for call in reply.calls})
        self.accepted_replies[reply.turn_id] = digest
        self.pending = None
        return True

    def start_call(self, call_id: str):
        record = self.calls[call_id]
        if record.status != "pending":
            return False
        record.status = "started"
        return True

    def complete_call(self, call_id: str, result: dict[str, Any]):
        record = self.calls[call_id]
        if record.status == "completed":
            if fingerprint(record.result) != fingerprint(result):
                raise ValueError("Completed observation cannot change")
            return
        if record.status != "started":
            raise ValueError("Call must be reserved before execution")
        record.result = deepcopy(result)
        record.status = "completed"

    def recover_interrupted_calls(self):
        for record in self.calls.values():
            if record.status == "started":
                record.result = {"status": "unknown", "reason": "execution_interrupted_no_automatic_retry"}
                record.status = "completed"
