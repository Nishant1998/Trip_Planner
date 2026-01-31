"""The single research-to-itinerary graph. Read this file to follow the workflow."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .adapters import adapt_research_report
from .planning_agent.agent import PlanningAgentLLM
from .planning_agent.application import build_application_runner
from .planning_agent.workflow import GuardedPlanningState, attach_guarded_workflow
from .planning_agent.tools import PlanningToolRuntime
from .research_agent.agent import ResearchAgentLLM
from .research_agent.nodes import build_research_nodes
from .research_agent.tools import ResearchToolRuntime
from .state import PipelineState
from .utils.logging import observe_node


async def handoff(state: PipelineState, *, source_reference="in_memory") -> dict:
    # JSON-shaped data preserves the original serialized report contract,
    # including dates and enums, without creating or reading an intermediate file.
    report = state["report"].model_dump(mode="json")
    if state["report"].status == "blocked" or not state["report"].quality.sufficient_for_planning:
        raise ValueError(
            "Research is insufficient for safe planner handoff; targeted repair was exhausted."
        )
    return {"input": adapt_research_report(report, source_reference=source_reference)}


def build_planning_graph(
    runtime: PlanningToolRuntime,
    agent: PlanningAgentLLM,
    *,
    stage_callback=None,
    planner_nodes=None,
    source_reference="in_memory",
):
    """Build only the planner path for a validated, persisted ResearchReport."""

    graph = StateGraph(GuardedPlanningState)
    async def handoff_then_initialize(state):
        return await handoff(state, source_reference=source_reference)
    graph.add_node("research_to_planner", observe_node("research_to_planner", handoff_then_initialize, stage_callback=stage_callback))
    graph.add_edge(START, "research_to_planner")
    if planner_nodes is not None:
        nodes = {name: observe_node(name, node, stage_callback=stage_callback, include_output=False)
                 for name, node in planner_nodes.items()}
        attach_guarded_workflow(graph, nodes, entry="research_to_planner")
    else:
        graph.add_node("planner_run", build_application_runner(agent, stage_callback=stage_callback))
        graph.add_edge("research_to_planner", "planner_run")
        graph.add_edge("planner_run", END)
    return graph.compile().with_config(recursion_limit=100)


def build_graph(
    runtime: ResearchToolRuntime,
    agent: ResearchAgentLLM,
    planning_runtime: PlanningToolRuntime,
    planning_agent: PlanningAgentLLM,
    *,
    stage_callback=None,
    research_report_callback=None,
):
    """Create the unchanged research loop followed by the guarded planner."""
    graph = StateGraph(GuardedPlanningState)
    nodes, after_agent, after_tools, after_validate = build_research_nodes(runtime, agent)
    async def persist_then_handoff(state):
        if research_report_callback:
            await research_report_callback(state["report"])
        return await handoff(state)

    nodes["research_to_planner"] = persist_then_handoff
    for name, node in nodes.items():
        graph.add_node(
            name,
            observe_node(
                name,
                node,
                stage_callback=stage_callback,
                include_output=name in {"research_to_planner", "planning_validate", "planning_finalize"},
            ),
        )

    graph.add_edge(START, "research_initialize")
    graph.add_edge("research_initialize", "research_agent")
    graph.add_conditional_edges("research_agent", after_agent, {"research_tools": "research_tools", "research_validate": "research_validate", "research_agent": "research_agent"})
    graph.add_conditional_edges("research_tools", after_tools, {"research_agent": "research_agent", "research_validate": "research_validate"})
    graph.add_conditional_edges("research_validate", after_validate, {"research_agent": "research_agent", "research_to_planner": "research_to_planner"})

    graph.add_node("planner_run", build_application_runner(planning_agent, stage_callback=stage_callback))
    graph.add_edge("research_to_planner", "planner_run")
    graph.add_edge("planner_run", END)
    return graph.compile().with_config(recursion_limit=150)
