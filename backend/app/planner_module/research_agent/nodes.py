"""Four-node LangGraph implementation of Research Agent V2."""

from __future__ import annotations

import logging
from typing import Any

from langgraph.prebuilt import ToolNode

from ..models.research import ResearchReport, ResearchRunMetrics
from .agent import ResearchAgentLLM
from .prompt import PROMPT_VERSION, build_agent_context
from .tools import ResearchToolRuntime
from .validator import finalise_report, validate_report

logger = logging.getLogger(__name__)


def _metrics(runtime: ResearchToolRuntime, agent: ResearchAgentLLM) -> ResearchRunMetrics:
    values = {
        **runtime.cache.metrics.model_dump(),
        **agent.metrics(),
        "provider_requests": dict(runtime.provider.provider_metrics),
        "checkpoint_count": runtime.checkpoint_count,
        "submission_count": runtime.submission_count,
        "duplicate_search_count": runtime.duplicate_search_count,
        "unsupported_tool_call_count": runtime.unsupported_tool_call_count,
        "invalid_tool_argument_count": runtime.invalid_tool_argument_count,
        "final_notebook_stop_count": len(runtime.notebook.resolved_stops),
        "final_notebook_poi_count": sum(
            len(items) for items in runtime.notebook.pois_by_stop.values()
        ),
    }
    return ResearchRunMetrics.model_validate(values)


def _fallback_report(state: dict[str, Any], runtime: ResearchToolRuntime, agent: ResearchAgentLLM) -> ResearchReport:
    report = runtime.compile_fallback_report(state["trip"])
    return report.model_copy(
        update={"metrics": _metrics(runtime, agent), "prompt_version": PROMPT_VERSION}
    )


def build_research_nodes(runtime: ResearchToolRuntime, agent: ResearchAgentLLM):
    tool_node = ToolNode(runtime.tools())

    async def initialize(state):
        runtime.reset()
        runtime.current_trip = state["trip"]
        if hasattr(agent, "reset"):
            agent.reset()
        return {"research_notebook": runtime.notebook, "research_observations": [], "research_evidence": [], "research_repair_count": 0, "research_agent_steps": 0, "validation_result": None, "candidate_report": None}

    async def agent_node(state):
        steps = state.get("research_agent_steps", 0)
        remaining = agent.config.max_agent_steps - steps
        if remaining <= 0 or agent.calls >= agent.config.max_llm_calls:
            runtime.candidate_report = _fallback_report(state, runtime, agent)
            runtime.submitted = True
            return {"research_agent_steps": steps, "candidate_report": runtime.candidate_report}
        messages = state.get("messages", [])
        context = build_agent_context(
            trip=state["trip"], notebook=runtime.notebook,
            observations=[item.model_dump(mode="json") for item in runtime.observations],
            feedback=state.get("validation_result"), remaining_steps=remaining,
            include_confirmed_trip=agent.calls == 0,
            remaining_budget=runtime.remaining_budget(
                remaining_llm_calls=agent.config.max_llm_calls - agent.calls,
                remaining_agent_steps=remaining,
            ),
        )
        logger.info(
            "research_agent_turn",
            extra={
                "agent_turn": steps + 1,
                "count": remaining,
                "checkpoint_count": runtime.checkpoint_count,
                "submission_count": runtime.submission_count,
            },
        )
        message = await agent.invoke(context, runtime.openai_schemas(), messages)
        return {
            "messages": [message],
            "research_agent_steps": steps + 1,
            "research_notebook": runtime.notebook,
            "research_observations": [
                item.model_dump(mode="json") for item in runtime.observations
            ],
            "research_evidence": [
                item.model_dump(mode="json") for item in runtime.evidence
            ],
        }

    async def validate_node(state):
        candidate = state.get("candidate_report") or runtime.candidate_report
        if candidate is None:
            return {"validation_result": {"outcome": "repair", "planner_usable": False, "issues": [{"code": "NO_SUBMISSION", "severity": "repair", "message": "Submit a report using the structured submission tool."}]}, "research_repair_count": state.get("research_repair_count", 0) + 1}
        result = validate_report(candidate, state["trip"], state.get("research_repair_count", 0), agent.config.max_repair_cycles)
        value = {"outcome": result.outcome, "planner_usable": result.planner_usable, "issues": result.issues}
        logger.info(
            "research_validation",
            extra={"status": result.outcome, "count": state.get("research_repair_count", 0)},
        )
        if result.outcome == "repair":
            runtime.submitted = False
            runtime.candidate_report = None
            return {"validation_result": value, "research_repair_count": state.get("research_repair_count", 0) + 1, "candidate_report": None}
        report = finalise_report(candidate, result, _metrics(runtime, agent), architecture_version="agentic_research_v2", prompt_version=PROMPT_VERSION)
        return {"validation_result": value, "report": report}

    def after_agent(state):
        if runtime.submitted:
            return "research_validate"
        last = (state.get("messages") or [None])[-1]
        return "research_tools" if getattr(last, "tool_calls", None) else "research_agent"

    def after_tools(state):
        return "research_validate" if runtime.submitted else "research_agent"

    def after_validate(state):
        return "research_agent" if (state.get("validation_result") or {}).get("outcome") == "repair" else "research_to_planner"

    return {"research_initialize": initialize, "research_agent": agent_node, "research_tools": tool_node, "research_validate": validate_node}, after_agent, after_tools, after_validate
