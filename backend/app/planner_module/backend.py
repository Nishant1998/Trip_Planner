"""Backend interface for the combined research and planning graph."""

from datetime import UTC, datetime
from time import monotonic

from app.models.input import TripMemoryInput
from app.models.progress import PlanningStage, ProgressEvent, ProgressEventKind

from .service import run, run_from_research_report

STAGES = {
    "prepare_state": ("building_itinerary", 65),
    "guard": ("building_itinerary", 65),
    "execute_tools": ("checking_routes", 80),
    "commit_and_check": ("reviewing_plan", 90),
    "finalize_result": ("finalizing", 96),
    "persist_and_present": ("finalizing", 97),
    "research_initialize": ("starting", 5),
    "research_agent": ("checking_destination", 20),
    "research_tools": ("finding_places", 40),
    "research_validate": ("reviewing_plan", 58),
    "research_to_planner": ("building_itinerary", 62),
    "planning_initialize": ("building_itinerary", 65),
    "planning_build_baseline": ("building_itinerary", 68),
    "planning_llm_propose": ("building_itinerary", 72),
    "planning_schedule": ("checking_routes", 84),
    "planning_llm_repair": ("reviewing_plan", 90),
    "planning_reschedule": ("finalizing", 94),
    "planning_agent": ("building_itinerary", 70),
    "planning_tools": ("checking_routes", 80),
    "planning_validate": ("reviewing_plan", 90),
    "planning_finalize": ("finalizing", 97),
    # Legacy progress aliases keep old clients and test harnesses readable
    # while the production graph uses the Planning Agent stages above.
    "prepare_planning": ("building_itinerary", 65),
    "plan_strategy": ("building_itinerary", 70),
    "plan_feasibility": ("checking_routes", 80),
    "plan_review": ("reviewing_plan", 90),
    "plan_final_safety": ("finalizing", 97),
}


class PlanningValidationError(ValueError):
    """Expose a rejected result to trusted diagnostic callers."""

    def __init__(self, result):
        super().__init__("The final itinerary did not pass required validation")
        self.result = result


class TripPlanningPipeline:
    async def generate(
        self,
        memory,
        *,
        request_id,
        progress_sink,
        traveler_context: dict[str, str] | None = None,
        research_report_callback=None,
    ):
        sequence = 0
        percent = 0
        started = monotonic()
        research_seconds = None

        async def stage_changed(name, action):
            nonlocal sequence, percent, research_seconds
            stage, milestone = STAGES[name]
            percent = max(percent, milestone)
            sequence += 1
            await progress_sink.publish(
                ProgressEvent(
                    run_id=request_id,
                    sequence=sequence,
                    stage=PlanningStage(stage),
                    kind=ProgressEventKind.STAGE_STARTED
                    if action == "started"
                    else ProgressEventKind.STAGE_COMPLETED,
                    occurred_at=datetime.now(UTC),
                    progress_percent=percent,
                    node=name,
                )
            )
            if name == "research_to_planner" and action == "started":
                research_seconds = monotonic() - started

        payload = (
            memory.model_dump(mode="json")
            if isinstance(memory, TripMemoryInput)
            else memory
        )
        if traveler_context:
            payload = {
                **payload,
                "internal": {
                    **(payload.get("internal") or {}),
                    "traveler_context": traveler_context,
                },
            }
        run_kwargs = {
            "request_id": request_id,
            "stage_callback": stage_changed,
        }
        if research_report_callback is not None:
            run_kwargs["research_report_callback"] = research_report_callback
        result = await run(payload, **run_kwargs)
        total = monotonic() - started
        result.timing_seconds = {
            "total": round(total, 3),
            "research": round(research_seconds or total, 3),
            "planner": round(total - (research_seconds or total), 3),
        }
        # A grounded but non-booking-ready itinerary is a user-visible draft,
        # not a system exception. Only a planner with no deliverable is blocked.
        if result.final_validation.get("delivery_status") == "BLOCKED":
            raise PlanningValidationError(result)
        return result

    async def generate_from_research_report(
        self,
        report,
        *,
        request_id,
        progress_sink,
    ):
        """Continue at planning when a persisted report is still usable."""

        sequence = 0
        percent = 58
        started = monotonic()

        async def stage_changed(name, action):
            nonlocal sequence, percent
            stage, milestone = STAGES[name]
            percent = max(percent, milestone)
            sequence += 1
            await progress_sink.publish(
                ProgressEvent(
                    run_id=request_id,
                    sequence=sequence,
                    stage=PlanningStage(stage),
                    kind=ProgressEventKind.STAGE_STARTED
                    if action == "started"
                    else ProgressEventKind.STAGE_COMPLETED,
                    occurred_at=datetime.now(UTC),
                    progress_percent=percent,
                    node=name,
                )
            )

        result = await run_from_research_report(
            report, request_id=request_id, stage_callback=stage_changed
        )
        result.timing_seconds = {
            "total": round(monotonic() - started, 3),
            "research": 0.0,
            "planner": round(monotonic() - started, 3),
        }
        if result.final_validation.get("delivery_status") == "BLOCKED":
            raise PlanningValidationError(result)
        return result

    async def close(self):
        return None
