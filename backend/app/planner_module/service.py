"""Public entry point: TripMemory in, researched itinerary out."""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from time import monotonic
from typing import Any
from uuid import uuid4

from .adapters import trip_memory_to_research_input
from .config import Settings
from .graph import build_graph, build_planning_graph
from .models.research import ResearchInput, ResearchReport
from .models.response import PipelineResult
from .planning_agent.agent import PlanningAgentLLM
from .planning_agent.config import PlanningAgentConfig
from .planning_agent.tools import PlanningToolRuntime
from .presentation import display_trip
from .providers import Provider
from .research_agent.agent import ResearchAgentLLM
from .research_agent.config import ResearchAgentConfig
from .research_agent.tools import ResearchToolRuntime
from .utils.logging import run_id
from .utils.services import PersistentResearchStore, PricingConfig, ResearchLimits

logger = logging.getLogger(__name__)


async def run(
    memory: dict[str, Any] | ResearchInput,
    *,
    settings: Settings | None = None,
    limits: ResearchLimits | None = None,
    pricing: PricingConfig | None = None,
    request_id: str | None = None,
    stage_callback=None,
    research_report_callback=None,
) -> PipelineResult:
    """Execute research and planning once, with no output-file persistence.

    The caller owns logging configuration and persistence of the returned object.
    Create a fresh graph and clients for each call to isolate budgets and caches.
    """
    settings = settings or Settings.from_environment()
    trip = (
        memory
        if isinstance(memory, ResearchInput)
        else trip_memory_to_research_input(memory)
    )
    limits = limits or ResearchLimits.from_environment()
    identifier = request_id or uuid4().hex
    started = monotonic()
    token = run_id.set(identifier)
    logger.info("pipeline_started")
    try:
        async with AsyncExitStack() as resources:
            provider = Provider.from_environment()
            resources.push_async_callback(provider.close)
            agent_config = ResearchAgentConfig.from_environment()
            agent = ResearchAgentLLM.from_environment(agent_config)
            resources.push_async_callback(agent.close)
            planning_agent = PlanningAgentLLM.from_environment(
                PlanningAgentConfig.from_environment()
            )
            resources.push_async_callback(planning_agent.close)
            persistent = PersistentResearchStore(limits.persistent_store_path)
            runtime = ResearchToolRuntime(provider, limits, persistent=persistent)
            graph = build_graph(
                runtime,
                agent,
                PlanningToolRuntime(),
                planning_agent,
                stage_callback=stage_callback,
                research_report_callback=research_report_callback,
            )
            recursion_limit = max(150, agent_config.max_agent_steps * 3 + 12)
            state = await graph.ainvoke(
                {"trip": trip, "settings": settings, "messages": []},
                config={"recursion_limit": recursion_limit},
            )

        result = PipelineResult(
            run_id=identifier,
            research_report=state["report"],
            planning_result=state["result"],
            final_validation=state["final_validation"],
            validation_issues=state["final_issues"],
            route_plan=state["route_plan"],
            display_trip=display_trip(
                state["result"],
                state["world"],
                state.get("mandatory_allocation", {}),
                state["final_validation"],
            ),
            metrics={
                "research": state["report"].metrics.model_dump(mode="json"),
                "planning": {
                    **state["result"].planning_metrics,
                    **state.get("metrics", {}),
                },
            },
            timing_seconds={"total": round(monotonic() - started, 3)},
        )
        logger.info(
            "pipeline_completed", extra={"status": result.final_validation["status"]}
        )
        return result
    except Exception:
        logger.exception("pipeline_failed")
        raise
    finally:
        run_id.reset(token)


async def run_from_research_report(
    report: dict[str, Any] | ResearchReport,
    *,
    settings: Settings | None = None,
    request_id: str | None = None,
    stage_callback=None,
) -> PipelineResult:
    """Plan from a previously validated persisted research report without re-researching."""

    research_report = (
        report if isinstance(report, ResearchReport) else ResearchReport.model_validate(report)
    )
    if research_report.status == "blocked" or not research_report.quality.sufficient_for_planning:
        raise ValueError("Saved research is not usable for planning.")
    settings = settings or Settings.from_environment()
    identifier = request_id or uuid4().hex
    started = monotonic()
    token = run_id.set(identifier)
    logger.info("planning_from_saved_research_started")
    try:
        planning_agent = PlanningAgentLLM.from_environment(
            PlanningAgentConfig.from_environment()
        )
        try:
            graph = build_planning_graph(
                PlanningToolRuntime(), planning_agent, stage_callback=stage_callback
            )
            state = await graph.ainvoke(
                {"report": research_report, "settings": settings}
            )
        finally:
            await planning_agent.close()
        result = PipelineResult(
            run_id=identifier,
            research_report=research_report,
            planning_result=state["result"],
            final_validation=state["final_validation"],
            validation_issues=state["final_issues"],
            route_plan=state["route_plan"],
            display_trip=display_trip(
                state["result"], state["world"],
                state.get("mandatory_allocation", {}), state["final_validation"],
            ),
            metrics={
                "research": research_report.metrics.model_dump(mode="json"),
                "planning": {**state["result"].planning_metrics, **state.get("metrics", {})},
            },
            timing_seconds={"total": round(monotonic() - started, 3), "research": 0.0},
        )
        logger.info("planning_from_saved_research_completed", extra={"status": result.final_validation["status"]})
        return result
    except Exception:
        logger.exception("planning_from_saved_research_failed")
        raise
    finally:
        run_id.reset(token)
