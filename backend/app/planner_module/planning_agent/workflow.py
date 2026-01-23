"""Guarded control flow with explicit planning and review invocation boundaries."""
from typing import Any, Awaitable, Callable, Literal

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from ..state import PipelineState
from .protocol import ModelReply


class GuardedPlanningState(PipelineState, total=False):
    planner_identity: dict[str, str]
    planner_state: dict[str, Any]
    planner_reply: dict[str, Any] | None
    planner_work_allowed: bool
    planner_finalize: bool
    planner_finalized: bool
    planner_persisted: bool
    planner_protocol_error: str | None
    planner_agent_role: str


NODE_NAMES = (
    "prepare_state", "guard", "planning_agent", "execute_tools",
    "commit_and_check", "finalize_result", "persist_and_present", "review_agent", "presentation_agent",
)


def build_guarded_workflow(
    nodes: dict[str, Callable[[GuardedPlanningState], Awaitable[dict]]], *, checkpointer=None,
):
    graph = StateGraph(GuardedPlanningState)
    attach_guarded_workflow(graph, nodes)
    return graph.compile(checkpointer=checkpointer)


def attach_guarded_workflow(graph, nodes, *, entry=START):
    """Use the same planner nodes in standalone and combined application graphs."""
    if set(nodes) != set(NODE_NAMES):
        raise ValueError("The planner requires all named node implementations")

    async def agent(state, name="planning_agent"):
        changes = await nodes[name](state)
        # Never route a prior turn's reply after a handled model error.
        changes = {"planner_reply": None, "planner_protocol_error": None, **changes}
        if changes["planner_reply"] is not None:
            try:
                ModelReply.model_validate(changes["planner_reply"])
            except ValidationError:
                changes["planner_reply"] = None
                changes["planner_protocol_error"] = "invalid_model_response"
        return changes

    async def reviewer(state):
        return await agent(state, "review_agent")

    async def finalize(state):
        changes = await nodes["finalize_result"](state)
        if changes.get("planner_finalized") is not True:
            raise RuntimeError("Final delivery gate did not validate an outcome")
        return changes

    async def persist(state):
        if not state.get("planner_finalized"):
            raise RuntimeError("Cannot publish before final validation")
        changes = await nodes["persist_and_present"](state)
        if changes.get("planner_persisted") is not True:
            raise RuntimeError("Final outcome was not durably persisted")
        return changes

    for name in NODE_NAMES:
        graph.add_node(name, {"planning_agent": agent, "review_agent": reviewer, "finalize_result": finalize,
                              "persist_and_present": persist}.get(name, nodes[name]))

    def after_guard(state) -> Literal["planning_agent", "review_agent", "finalize_result"]:
        if state.get("planner_work_allowed") is not True:
            return "finalize_result"
        return "review_agent" if state.get("planner_agent_role") == "review_and_repair" else "planning_agent"

    def after_agent(state) -> Literal["execute_tools", "commit_and_check", "guard"]:
        raw = state.get("planner_reply")
        if raw is None:
            return "guard"
        reply = ModelReply.model_validate(raw)
        return "execute_tools" if reply.calls else "commit_and_check"

    def after_check(state) -> Literal["finalize_result", "guard"]:
        return "finalize_result" if state.get("planner_finalize") is True else "guard"

    graph.add_edge(entry, "prepare_state")
    graph.add_edge("prepare_state", "guard")
    graph.add_conditional_edges("guard", after_guard)
    graph.add_conditional_edges("planning_agent", after_agent)
    graph.add_conditional_edges("review_agent", after_agent)
    graph.add_edge("execute_tools", "guard")
    graph.add_conditional_edges("commit_and_check", after_check)
    graph.add_edge("finalize_result", "presentation_agent")
    graph.add_edge("presentation_agent", "persist_and_present")
    graph.add_edge("persist_and_present", END)
