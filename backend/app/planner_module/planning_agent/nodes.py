"""Bounded Planner V2 graph nodes; the model never controls the workflow."""

from __future__ import annotations

from copy import deepcopy
import asyncio
import logging

from ..adapters import build_world
from .agent import PlanningAgentLLM
from .baseline import build_safe_skeleton
from .models import PlanProposal, PlanningCheckpoint, PlanningNotebook, SemanticDayDraft
from .prompt import PROMPT_VERSION, build_agent_context
from .tools import PlanningToolRuntime

logger = logging.getLogger(__name__)


def build_planning_nodes(runtime: PlanningToolRuntime, agent: PlanningAgentLLM):
    def proposal_schema():
        return {
            "type": "function",
            "name": "submit_plan_proposal",
            "description": "Propose a complete researched journey: all days, bases, mandatory and optional visits, and preferred order.",
            "parameters": PlanProposal.model_json_schema(),
        }

    def merge_proposal(proposal: PlanProposal, *, repair: bool = False) -> PlanningCheckpoint:
        """A full semantic replacement is accepted only by checkpoint validation."""
        return PlanningCheckpoint(
            base_sequence=proposal.base_sequence,
            nights_by_base=proposal.nights_by_base,
            stay_area_by_night=proposal.stay_area_by_night,
            days=sorted(proposal.days, key=lambda day: day.day_number),
            alternatives=proposal.alternatives,
            unresolved_items=list(dict.fromkeys([
                *runtime.notebook.unresolved_items, *proposal.unresolved_items,
            ])),
            guidance_items=list(dict.fromkeys([
                *runtime.notebook.guidance_items, *proposal.guidance_items,
            ])),
        )

    async def initialize(state):
        runtime.reset(build_world(state["input"]), state["settings"])
        agent.reset()
        return {"world": runtime.world, "planning_repair_count": 0}

    async def baseline(_state):
        strategy = build_safe_skeleton(runtime.world)
        runtime.notebook = PlanningNotebook(
            plan_version=1, base_sequence=strategy.base_sequence,
            nights_by_base=strategy.nights_by_base,
            days=[SemanticDayDraft(day_number=day.day_number, base_stop_id=day.base_stop_id,
                 day_kind=day.kind, theme=day.theme,
                 selected_poi_ids=day.candidate_poi_ids) for day in strategy.days],
        )
        runtime.completion_path = "safe_baseline"
        runtime.checkpoint_plan(PlanningCheckpoint(
            base_sequence=runtime.notebook.base_sequence,
            nights_by_base=runtime.notebook.nights_by_base,
            days=runtime.notebook.days,
        ))
        return {"planning_notebook": runtime.notebook}

    async def propose(_state):
        if agent.available and runtime.remaining_runtime_seconds() > agent.config.finalization_reserve_seconds:
            context = build_agent_context(world=runtime.world, notebook=runtime.notebook,
                feedback=None, remaining_steps=1, include_input=True)
            remaining = runtime.remaining_runtime_seconds() or 0
            try:
                response = await asyncio.wait_for(
                    agent.invoke(context, [proposal_schema()], []),
                    timeout=max(1, remaining - agent.config.finalization_reserve_seconds),
                )
            except (asyncio.TimeoutError, TimeoutError):
                runtime.fallback_reason = "proposal_deadline_reached"
                return {"planning_agent_steps": 0}
            except Exception as error:
                # Quota, schema and transient model failures may not erase a
                # deterministic itinerary assembled from sufficient Research.
                runtime.fallback_reason = "proposal_unavailable"
                logger.warning("planner_proposal_unavailable", extra={"error_type": type(error).__name__})
                return {"planning_agent_steps": 0}
            call = next((call for call in response.tool_calls if call.get("name") == "submit_plan_proposal"), None)
            if call:
                try:
                    proposal = PlanProposal.model_validate(call.get("args") or {})
                    merged = runtime.checkpoint_plan(merge_proposal(proposal))
                    if merged["status"] == "ok":
                        runtime.completion_path = "llm_proposal_merged"
                    else:
                        runtime.fallback_reason = "proposal_checkpoint_invalid"
                        logger.warning("planner_proposal_rejected", extra={"issue_codes": [item["code"] for item in merged.get("issues", [])]})
                except Exception:
                    # A malformed model proposal has no authority to replace the
                    # already-valid deterministic safe skeleton.
                    runtime.fallback_reason = "proposal_schema_invalid"
        return {"planning_agent_steps": 1}

    async def schedule(_state):
        remaining = runtime.remaining_runtime_seconds() or 0
        deadline = remaining - agent.config.finalization_reserve_seconds
        if deadline <= 0:
            runtime.build_deadline_draft()
            runtime.submitted = True
            return {"planning_candidate_result": runtime.candidate_result}
        try:
            # This is the factual stage with the greatest provider exposure.
            # Its deadline covers the whole planner runtime, leaving the final
            # reserve for an I/O-free safe draft and presentation.
            await asyncio.wait_for(runtime.validate_trip(include_budget=False), timeout=deadline)
        except (asyncio.TimeoutError, TimeoutError):
            runtime.build_deadline_draft()
        runtime.candidate_result = runtime.last_validation
        runtime.submitted = True
        return {"planning_candidate_result": runtime.candidate_result}

    async def repair(_state):
        validation = runtime.last_validation or {}
        issues = validation.get("issues", [])
        route_blocked_days = {item.day_number for item in issues if item.code == "CRITICAL_ROUTE_UNKNOWN"}
        repairable = [
            item for item in issues
            if item.code not in {"CRITICAL_ROUTE_UNKNOWN"}
            and not (item.code == "MISSING_MANDATORY_POI" and any(
                day.day_number in route_blocked_days and any(
                    item.message == f"Mandatory POI not scheduled: {poi.name}"
                    for poi in day.provisional_activities if poi.mandatory
                ) for day in validation.get("result").days
            ))
            and item.severity == "hard"
            and item.code in {
                "DAY_END_HARD_EXCEEDED", "DAY_DURATION_UNREALISTIC",
                "MANDATORY_UNSCHEDULABLE_ASSIGNED_POI", "MISSING_MANDATORY_POI",
                "EMPTY_SIGHTSEEING_DAY",
            }
        ]
        if not repairable or not agent.available:
            return {"planning_repair_count": 0}
        remaining = runtime.remaining_runtime_seconds() or 0
        if remaining <= agent.config.finalization_reserve_seconds:
            runtime.fallback_reason = runtime.fallback_reason or "repair_deadline_reached"
            return {"planning_repair_count": 0}
        runtime.pre_repair_candidate = deepcopy(runtime.last_validation)
        runtime.pre_repair_notebook = runtime.notebook.model_copy(deep=True)
        feedback = {"issues": [item.model_dump(mode="json") for item in repairable]}
        context = build_agent_context(world=runtime.world, notebook=runtime.notebook,
            feedback=feedback, remaining_steps=1, include_input=True)
        try:
            response = await asyncio.wait_for(
                agent.invoke(context, [proposal_schema()], []),
                timeout=max(1, remaining - agent.config.finalization_reserve_seconds),
            )
            call = next((call for call in response.tool_calls if call.get("name") == "submit_plan_proposal"), None)
            if call:
                proposal = PlanProposal.model_validate(call.get("args") or {})
                merged = runtime.checkpoint_plan(merge_proposal(proposal, repair=True))
                if merged["status"] == "ok":
                    runtime.completion_path = "llm_repair_merged"
                    return {"planning_repair_count": 1}
        except (asyncio.TimeoutError, TimeoutError):
            runtime.fallback_reason = runtime.fallback_reason or "repair_deadline_reached"
        except Exception as error:
            runtime.fallback_reason = runtime.fallback_reason or "repair_schema_invalid"
            logger.warning("planner_repair_failed", extra={"error_type": type(error).__name__})
        return {"planning_repair_count": 0}

    async def reschedule(state):
        if runtime.fallback_reason == "planner_deadline_reached":
            runtime.candidate_result = runtime.last_validation
            runtime.submitted = True
            return {"planning_candidate_result": runtime.candidate_result}
        # No repair means the first factual schedule remains authoritative; do
        # not route/materialize it again merely to attach the final budget.
        if state.get("planning_repair_count", 0):
            remaining = (runtime.remaining_runtime_seconds() or 0) - agent.config.finalization_reserve_seconds
            try:
                if remaining <= 0:
                    raise asyncio.TimeoutError
                await asyncio.wait_for(runtime.validate_trip(include_budget=True), timeout=remaining)
                if runtime.candidate_rank(runtime.last_validation) < runtime.candidate_rank(runtime.pre_repair_candidate):
                    runtime.last_validation = runtime.pre_repair_candidate
                    runtime.notebook = runtime.pre_repair_notebook
                    runtime.fallback_reason = "repair_regressed"
                    runtime.finalize_current_candidate_budget()
            except (asyncio.TimeoutError, TimeoutError):
                runtime.last_validation = runtime.pre_repair_candidate
                runtime.notebook = runtime.pre_repair_notebook
                runtime.fallback_reason = "repair_deadline_reached"
                runtime.finalize_current_candidate_budget()
            except Exception:
                logger.exception("planner_repair_materialization_failed")
                runtime.last_validation = runtime.pre_repair_candidate
                runtime.notebook = runtime.pre_repair_notebook
                runtime.fallback_reason = "repair_materialization_failed"
                runtime.finalize_current_candidate_budget()
        else:
            runtime.finalize_current_candidate_budget()
        runtime.candidate_result = runtime.last_validation
        runtime.submitted = True
        return {"planning_candidate_result": runtime.candidate_result}

    async def finalize(state):
        candidate = runtime.candidate_result
        result, issues, route_plan, mandatory, validation = (
            candidate["result"], candidate["issues"], candidate["route_plan"],
            candidate["mandatory"], candidate["validation"],
        )
        result.planning_metrics.update({**runtime.metrics(), **agent.metrics(),
            "prompt_version": PROMPT_VERSION, "final_status": validation["status"]})
        result.status = "complete" if validation["publishable"] else "best_effort"
        return {"result": result, "final_issues": issues, "route_plan": route_plan,
                "mandatory_allocation": mandatory, "final_validation": validation,
                "metrics": {**state.get("metrics", {}), **runtime.metrics(), **agent.metrics()}}

    return {"planning_initialize": initialize, "planning_build_baseline": baseline,
            "planning_llm_propose": propose, "planning_schedule": schedule,
            "planning_llm_repair": repair, "planning_reschedule": reschedule,
            "planning_finalize": finalize}
