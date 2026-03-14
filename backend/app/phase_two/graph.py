"""Six-node LangGraph orchestration with strict agent update ownership.

Specialist callbacks are injected so provider/model failures can be tested
without network calls. A role-scoped LLM chooses an eligible routing decision;
the deterministic fallback preserves dependencies and termination on failure.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.planning_tools.contracts import RouteLeg
from langgraph.graph import END, START, StateGraph
from pydantic import TypeAdapter

from .contracts import (
    AccommodationRecord,
    AgentProgress,
    AgentTaskState,
    CostRecord,
    DestinationRecord,
    JourneyRequest,
    PlaceRecord,
    PlannedDay,
    PlanWarning,
    RouteRequirement,
    RoutingState,
    RunMeta,
    StayRequest,
    TransportRecord,
    TripPlanV2,
    TripRequest,
    TripState,
    TripSummary,
    WeatherRecord,
)
from .itinerary_fallback import fallback_days
from .validation import validate_plan

Specialist = Callable[[TripState], Awaitable[Mapping[str, Any]]]
ItinerarySpecialist = Callable[[TripState, str], Awaitable[Mapping[str, Any]]]
PlanStore = Callable[[TripPlanV2], Awaitable[str]]

AGENTS = (
    "supervisor",
    "destination_research",
    "itinerary_planning",
    "accommodation",
    "transport_booking",
    "response_aggregator",
)
_TASKS = ("research", "allocation", "accommodation", "transport", "schedule")
_NEXT = {
    "research": "destination_research",
    "allocation": "itinerary_planning",
    "accommodation": "accommodation",
    "transport": "transport_booking",
    "schedule": "itinerary_planning",
}
_ALLOWED = {
    "research": {"destinations", "places", "weather", "warnings"},
    "allocation": {
        "stay_requests",
        "journeys",
        "route_requirements",
        "itinerary",
        "warnings",
    },
    "accommodation": {"hotels", "pending_jobs", "warnings"},
    "transport": {"transport_options", "route_matrix", "route_profiles", "pending_jobs", "warnings"},
    "schedule": {
        "itinerary", "stay_requests", "journeys", "route_requirements", "warnings"
    },
}
_SCHEMAS: dict[str, TypeAdapter] = {
    "route_profiles": TypeAdapter(dict[str, Any]),
    "destinations": TypeAdapter(list[DestinationRecord]),
    "places": TypeAdapter(list[PlaceRecord]),
    "weather": TypeAdapter(list[WeatherRecord]),
    "stay_requests": TypeAdapter(list[StayRequest]),
    "hotels": TypeAdapter(list[AccommodationRecord]),
    "transport_options": TypeAdapter(list[TransportRecord]),
    "route_matrix": TypeAdapter(list[RouteLeg]),
    "itinerary": TypeAdapter(list[PlannedDay]),
    "costs": TypeAdapter(CostRecord),
    "warnings": TypeAdapter(list[PlanWarning]),
    "journeys": TypeAdapter(list[JourneyRequest]),
    "route_requirements": TypeAdapter(list[RouteRequirement]),
    "pending_jobs": TypeAdapter(dict[str, Any]),
}


@dataclass
class GraphHandlers:
    research: Specialist | None = None
    itinerary: ItinerarySpecialist | None = None
    accommodation: Specialist | None = None
    transport: Specialist | None = None
    persist: PlanStore | None = None
    supervisor: Callable | None = None
    presentation: Callable | None = None
    progress: Callable | None = None


def initial_state(
    request: TripRequest,
    *,
    run_id: str,
    started_at: datetime | None = None,
    deadline_seconds: int = 300,
    planning_profile: str = "full",
) -> TripState:
    started = started_at or datetime.now(UTC)
    run = RunMeta(
        planning_profile=planning_profile,
        run_id=run_id,
        trip_id=request.trip_id,
        snapshot_id=request.snapshot_id,
        started_at=started,
        deadline_at=started + timedelta(seconds=deadline_seconds),
    )
    return TripState(
        route_profiles={},
        run=run,
        user_preferences=request,
        destinations=[],
        places=[],
        weather=[],
        stay_requests=[],
        hotels=[],
        transport_options=[],
        route_matrix=[],
        itinerary=[],
        costs=CostRecord(
            currency=request.currency,
            unknown_components=["accommodation", "transport", "activities"],
        ),
        warnings=[],
        agent_status={task: AgentProgress() for task in _TASKS},
        routing=RoutingState(),
        final_plan=None,
        persistence={},
        journeys=[],
        route_requirements=[],
        validation=[],
        pending_jobs={},
    )


def _task_fingerprint(state: TripState, task: str) -> str:
    relevant = {
        "snapshot": state["run"].snapshot_id,
        "task": task,
        "issues": [
            (issue.code, issue.affected_id)
            for issue in state["validation"]
            if issue.owner == task or task == "schedule"
        ],
        "stays": [s.model_dump(mode="json") for s in state["stay_requests"]]
        if task == "accommodation" else [],
        "journeys": [j.model_dump(mode="json") for j in state["journeys"]]
        if task == "transport" else [],
        "routes": [r.model_dump(mode="json") for r in state["route_requirements"]]
        if task == "transport" else [],
    }
    return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()


def _eligible_tasks(state: TripState) -> list[str]:
    status = state["agent_status"]
    routing = state["routing"]
    if (routing.delegation_count >= 8 or routing.supervisor_decisions >= 9
            or datetime.now(UTC) >= state["run"].deadline_at - timedelta(seconds=30)):
        return ["finish"]
    for task in ("research", "allocation"):
        if status[task].status == AgentTaskState.PENDING:
            return [task]
    independent = []
    if status["accommodation"].status == AgentTaskState.PENDING and state["stay_requests"]:
        independent.append("accommodation")
    if status["transport"].status == AgentTaskState.PENDING and (
        state["journeys"] or state["route_requirements"]
    ):
        independent.append("transport")
    if len(independent) == 2:
        place_ids = {p.id for p in state["places"]}
        needs_hotel = any(
            r.origin_id not in place_ids or r.destination_id not in place_ids
            for r in state["route_requirements"]
        )
        if needs_hotel:
            return ["accommodation"]
    if independent:
        return independent
    if status["schedule"].status == AgentTaskState.PENDING:
        return ["schedule"]
    if routing.repair_started:
        return ["schedule"] if routing.repair_stage == "specialist" else ["finish"]
    repair_issues = [i for i in state["validation"] if i.severity == "error"]
    repair_issues.sort(key=lambda i: i.owner in ("allocation", "schedule"))
    for issue in repair_issues:
        owner = issue.owner if issue.owner not in ("allocation", "schedule") else "schedule"
        progress = status[owner]
        if progress.attempts >= (3 if owner == "schedule" else 2):
            continue
        if owner != "schedule" and progress.input_fingerprint == _task_fingerprint(state, owner):
            continue
        return [owner]
    return ["finish"]


def _merge_by_id(previous: list, changed: list, key) -> list:
    merged = {key(item): item for item in previous}
    merged.update((key(item), item) for item in changed)
    return list(merged.values())


def _merge_agent_update(
    state: TripState, task: str, update: dict[str, Any],
    status: dict[str, AgentProgress],
) -> dict[str, Any]:
    """Apply targeted repair patches and remove evidence tied to changed inputs."""
    update = dict(update)
    if "route_requirements" in update:
        update["route_requirements"] = _merge_by_id(
            state["route_requirements"], update["route_requirements"],
            lambda r: (r.origin_id, r.destination_id, r.mode),
        )
    if not state["routing"].repair_started:
        if task == "schedule" and ("stay_requests" in update or "journeys" in update):
            raise ValueError("schedule may revise stays or journeys only during repair")
        return update
    collections = {
        "research": (("destinations", lambda d: d.id),
                     ("places", lambda p: p.id),
                     ("weather", lambda w: (w.destination_id, w.date))),
        "accommodation": (("hotels", lambda h: h.stay_request.id),),
        "transport": (("transport_options", lambda t: t.id),
                      ("route_matrix", lambda r: (r.origin_id, r.destination_id, r.mode))),
        "schedule": (("itinerary", lambda d: d.day),
                     ("stay_requests", lambda s: s.id),
                     ("journeys", lambda j: j.id)),
    }
    for field, key in collections.get(task, ()):
        if field in update:
            update[field] = _merge_by_id(state[field], update[field], key)
    if task != "schedule":
        if task in ("accommodation", "transport") and "pending_jobs" in update:
            update["pending_jobs"] = {**state["pending_jobs"], **update["pending_jobs"]}
        return update
    if "stay_requests" in update:
        old = {s.id: s for s in state["stay_requests"]}
        changed_ids = {s.id for s in update["stay_requests"] if old.get(s.id) != s}
        if changed_ids:
            stale_hotels = [h.selected.id for h in state["hotels"]
                            if h.stay_request.id in changed_ids and h.selected]
            update["hotels"] = [h for h in state["hotels"]
                                if h.stay_request.id not in changed_ids]
            update["route_matrix"] = [r for r in state["route_matrix"]
                                      if r.origin_id not in stale_hotels
                                      and r.destination_id not in stale_hotels]
            update["route_requirements"] = [r for r in update.get(
                "route_requirements", state["route_requirements"])
                if r.origin_id not in stale_hotels and r.destination_id not in stale_hotels]
            if status["accommodation"].attempts < 2:
                status["accommodation"].status = AgentTaskState.PENDING
    if "journeys" in update:
        old = {j.id: j for j in state["journeys"]}
        changed_ids = {j.id for j in update["journeys"] if old.get(j.id) != j}
        if changed_ids:
            update["transport_options"] = [t for t in state["transport_options"]
                                           if t.id not in changed_ids]
            if status["transport"].attempts < 2:
                status["transport"].status = AgentTaskState.PENDING
    return update


def _merge_warnings(
    existing: list[PlanWarning], incoming: list[PlanWarning]
) -> list[PlanWarning]:
    merged = {(warning.code, warning.affected_id): warning for warning in existing}
    for warning in incoming:
        merged[(warning.code, warning.affected_id)] = warning
    return list(merged.values())


def _validated_update(task: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    unexpected = set(raw) - _ALLOWED[task]
    if unexpected:
        raise ValueError(
            f"{task} tried to write fields outside its role: {sorted(unexpected)}"
        )
    return {key: _SCHEMAS[key].validate_python(value) for key, value in raw.items()}


def build_graph(handlers: GraphHandlers | None = None, *, checkpointer=None):
    """Compile exactly the six approved agent nodes (no helper nodes)."""
    handlers = handlers or GraphHandlers()

    async def supervisor(state: TripState) -> dict[str, Any]:
        eligible = _eligible_tasks(state)
        task = eligible[0] if eligible != ["finish"] else None
        routing = state["routing"].model_copy(deep=True)
        status = {key: value.model_copy(deep=True)
                  for key, value in state["agent_status"].items()}
        remaining = (state["run"].deadline_at - datetime.now(UTC)).total_seconds()
        warnings = list(state["warnings"])
        if (
            routing.delegation_count >= 8
            or routing.supervisor_decisions >= 9
            or remaining <= 30
        ):
            task = None
        if handlers.supervisor and remaining > 35 and routing.supervisor_decisions < 9:
            try:
                async with asyncio.timeout(min(30, remaining - 30)):
                    decision = await handlers.supervisor(
                        copy.deepcopy(state), eligible
                    )
                if decision.task not in eligible:
                    raise ValueError("ineligible routing decision")
                task = None if decision.task == "finish" else decision.task
                routing.reason = decision.reason
            except Exception:  # noqa: BLE001 - model boundary: preserve deterministic fallback
                warnings.append(
                    PlanWarning(
                        code="supervisor_fallback",
                        source="supervisor",
                        message="Routing model unavailable or ineligible decision; dependency-safe routing was used.",
                    )
                )
        if task and status["schedule"].attempts and not routing.repair_started:
            routing.repair_started = True
            routing.repair_stage = "schedule" if task == "schedule" else "specialist"
        elif task == "schedule" and routing.repair_stage == "specialist":
            routing.repair_stage = "schedule"
        if status["allocation"].status != AgentTaskState.PENDING:
            if not state["stay_requests"] and status["accommodation"].status == AgentTaskState.PENDING:
                status["accommodation"].status = AgentTaskState.SKIPPED
            if (not state["journeys"] and not state["route_requirements"]
                    and status["transport"].status == AgentTaskState.PENDING):
                status["transport"].status = AgentTaskState.SKIPPED
        routing.supervisor_decisions += 1
        routing.task = task
        routing.next_agent = _NEXT[task] if task else "response_aggregator"
        if task:
            routing.delegation_count += 1
        else:
            routing.completion_reason = "tasks_finished_or_budget_exhausted"
        return {"routing": routing, "agent_status": status,
                "warnings": _merge_warnings([], warnings)}

    def route(state: TripState) -> str:
        return state["routing"].next_agent or "response_aggregator"

    async def execute(
        state: TripState,
        task: str,
        callback: Specialist | None,
        itinerary_callback: ItinerarySpecialist | None = None,
    ) -> dict[str, Any]:
        status = {key: value.model_copy(deep=True)
                  for key, value in state["agent_status"].items()}
        progress = status[task].model_copy(deep=True)
        progress.attempts += 1
        progress.input_fingerprint = _task_fingerprint(state, task)
        warnings: list[PlanWarning] = []
        try:
            remaining = (
                state["run"].deadline_at - datetime.now(UTC)
            ).total_seconds() - 30
            if remaining <= 0:
                raise TimeoutError("planning budget exhausted")
            if handlers.progress:
                await handlers.progress(
                    task, routing_count=state["routing"].delegation_count
                )
            if itinerary_callback:
                raw = await asyncio.wait_for(
                    itinerary_callback(copy.deepcopy(state), task), remaining
                )
            elif callback:
                raw = await asyncio.wait_for(callback(copy.deepcopy(state)), remaining)
            else:
                raw = {}
            update = _validated_update(task, raw)
            warnings = update.pop("warnings", [])
            update = _merge_agent_update(state, task, update, status)
            progress.status = (
                AgentTaskState.PARTIAL
                if warnings
                else AgentTaskState.COMPLETE
                if raw
                else AgentTaskState.SKIPPED
            )
        except Exception:  # noqa: BLE001 - specialist failure must not abort a best-effort plan
            update = {}
            warnings = [
                PlanWarning(
                    code=f"{task}_failed",
                    source=_NEXT[task],
                    affected_id=task,
                    message=f"{task.capitalize()} could not complete; the plan will continue with available data.",
                )
            ]
            progress.status = AgentTaskState.PARTIAL
        if (task == "schedule" and not update.get("itinerary")
                and not state["routing"].repair_started):
            request = state["user_preferences"]
            expected_dates = [request.start_date + timedelta(days=n)
                              for n in range((request.end_date - request.start_date).days + 1)]
            retain_allocation = [d.date for d in state["itinerary"]] == expected_dates
            update["itinerary"] = (copy.deepcopy(state["itinerary"]) if retain_allocation
                                   else fallback_days(request))
            warnings.append(
                PlanWarning(
                    code="allocation_retained" if retain_allocation else "itinerary_fallback",
                    source="itinerary_planning",
                    affected_id=None,
                    message=("The allocated activities are retained as a draft; final scheduling did not complete."
                             if retain_allocation else
                             "A day-by-day outline is shown because a verified schedule is unavailable."),
                )
            )
            progress.status = AgentTaskState.PARTIAL
        status[task] = progress
        if task == "schedule":
            candidate = {**state, **update}
            issues, costs = validate_plan(candidate)
            update["validation"], update["costs"] = issues, costs
            # Invalid date coverage cannot escape as an invalid final JSON.
            if any(i.code == "date_coverage" for i in issues):
                update["itinerary"] = fallback_days(state["user_preferences"])
                progress.status = AgentTaskState.PARTIAL
            if issues:
                progress.status = AgentTaskState.PARTIAL
        return {
            **update,
            "warnings": _merge_warnings(state["warnings"], warnings),
            "agent_status": status,
        }

    async def research(state: TripState) -> dict[str, Any]:
        return await execute(state, "research", handlers.research)

    async def itinerary(state: TripState) -> dict[str, Any]:
        task = state["routing"].task
        if task not in {"allocation", "schedule"}:
            raise ValueError("Itinerary was called without an eligible task")
        return await execute(state, task, None, handlers.itinerary)

    async def accommodation(state: TripState) -> dict[str, Any]:
        return await execute(state, "accommodation", handlers.accommodation)

    async def transport(state: TripState) -> dict[str, Any]:
        return await execute(state, "transport", handlers.transport)

    async def aggregator(state: TripState) -> dict[str, Any]:
        """Format accepted state only; scheduling never happens here."""
        request, run = state["user_preferences"], state["run"]
        warnings = list(state["warnings"])
        if not state["itinerary"]:
            state = {**state, "itinerary": fallback_days(request)}
        issues, costs = validate_plan(state)
        if any(i.code == "date_coverage" for i in issues):
            state = {**state, "itinerary": fallback_days(request)}
        warnings = _merge_warnings(
            warnings,
            [
                PlanWarning(
                    code=i.code,
                    message=i.message,
                    source="validation",
                    affected_id=i.affected_id,
                    severity=i.severity,
                )
                for i in issues
            ],
        )
        title = "Trip to " + ", ".join(request.destinations)
        activities = sum(event.kind == "activity" for day in state["itinerary"] for event in day.events)
        party = f"{request.adults} {'adult' if request.adults == 1 else 'adults'}"
        if request.children:
            party += f" and {request.children} {'child' if request.children == 1 else 'children'}"
        overview = (
            f"This {len(state['itinerary'])}-day draft is for your requested trip to "
            f"{', '.join(request.destinations)} with {party}. "
            + (f"It contains {activities} sightseeing activities arranged by day, not confirmed appointment times. "
               if activities else "Sightseeing still needs planning; this outline is not a finished itinerary. ")
            + "Review unresolved items and confirm bookings and total costs before travelling."
        )
        if handlers.presentation:
            try:
                remaining = (run.deadline_at - datetime.now(UTC)).total_seconds() - 5
                if remaining <= 0:
                    raise TimeoutError("finalization budget exhausted")
                presentation = await asyncio.wait_for(
                    handlers.presentation(
                        copy.deepcopy(
                            {
                                **state,
                                "warnings": warnings,
                                "costs": costs,
                                "validation": issues,
                            }
                        )
                    ),
                    min(25, remaining),
                )
                title, overview = presentation.title, presentation.overview
            except Exception:  # noqa: BLE001 - narrative is optional; retain validated facts
                warnings.append(
                    PlanWarning(
                        code="presentation_fallback",
                        source="response_aggregator",
                        message="Narrative generation unavailable; the structured plan and warnings remain available.",
                    )
                )
        plan = TripPlanV2(
            planning_profile=run.planning_profile,
            route_profiles=state.get("route_profiles", {}),
            plan_id=f"{run.run_id}:v2",
            trip_id=run.trip_id,
            run_id=run.run_id,
            status="partial" if warnings or costs.unknown_components else "complete",
            trip_summary=TripSummary(
                title=title,
                overview=overview,
                assumptions=request.assumptions,
                start_date=request.start_date,
                end_date=request.end_date,
                adults=request.adults,
                children=request.children,
                requested_destinations=request.destinations,
                origin=request.origin,
                budget=request.budget,
                currency=request.currency,
            ),
            destinations=state["destinations"],
            accommodation=state["hotels"],
            transport=state["transport_options"],
            days=state["itinerary"],
            total_estimated_cost=costs,
            warnings=warnings,
            generated_at=datetime.now(UTC),
            weather=state["weather"],
        )
        persistence: dict[str, Any] = {"status": "unavailable"}
        if handlers.persist:
            try:
                persistence = {
                    "status": "saved",
                    "plan_id": await asyncio.wait_for(handlers.persist(plan), 5),
                }
            except Exception:  # noqa: BLE001 - storage failure is exposed explicitly in result
                persistence = {"status": "failed"}
                warnings.append(
                    PlanWarning(
                        code="storage_failed",
                        source="response_aggregator",
                        affected_id=plan.plan_id,
                        message="The plan was generated but could not be saved.",
                    )
                )
                plan = plan.model_copy(
                    update={"status": "partial", "warnings": warnings}
                )
        return {"final_plan": plan, "persistence": persistence, "warnings": warnings}

    graph = StateGraph(TripState)
    graph.add_node("supervisor", supervisor)
    graph.add_node("destination_research", research)
    graph.add_node("itinerary_planning", itinerary)
    graph.add_node("accommodation", accommodation)
    graph.add_node("transport_booking", transport)
    graph.add_node("response_aggregator", aggregator)
    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges(
        "supervisor",
        route,
        {
            "destination_research": "destination_research",
            "itinerary_planning": "itinerary_planning",
            "accommodation": "accommodation",
            "transport_booking": "transport_booking",
            "response_aggregator": "response_aggregator",
        },
    )
    for specialist in (
        "destination_research",
        "itinerary_planning",
        "accommodation",
        "transport_booking",
    ):
        graph.add_edge(specialist, "supervisor")
    graph.add_edge("response_aggregator", END)
    return graph.compile(checkpointer=checkpointer)
