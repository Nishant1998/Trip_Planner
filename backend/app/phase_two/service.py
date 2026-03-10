"""Feature-gated Phase 1 confirmation -> six-agent job -> saved plan."""

import asyncio
import hashlib
import json
import os

from app.models.state import load_runtime_memory
from app.planning_tools.catalog import PlanningToolCatalog
from app.providers.tool_loop import OpenAIResponsesToolModel
from app.services.progress import ProgressReporter

from .agents import AGENT_TEMPLATES
from .graph import GraphHandlers, build_graph, initial_state
from .input_adapter import request_from_confirmed_memory
from .persistence import CollectionJournal, PostgresControlCheckpointer
from .runtime import SixAgentRuntime
from .store import PlanningStore


class PlanningProgressSink:
    def __init__(self, bus, trip_id):
        self.bus, self.trip_id = bus, trip_id

    async def publish(self, event):
        # Only typed stage/status messages, never raw arguments/provider data.
        await self.bus.publish(
            self.trip_id, "planning_progress", event.model_dump(mode="json")
        )


class PlanningService:
    def __init__(
        self,
        repository,
        event_bus,
        *,
        store=None,
        model_factory=None,
        catalog_factory=None,
        enabled=None,
        planning_profile="full",
    ):
        self.repository, self.event_bus = repository, event_bus
        if planning_profile not in ("full", "daywise", "discovery"):
            raise ValueError("Unknown planning profile")
        self.planning_profile = planning_profile
        self.store = store or PlanningStore(repository)
        self.enabled = (
            enabled
            if enabled is not None
            else os.getenv("PHASE_TWO_ENABLED", "false").lower() == "true"
        )
        self.model_factory = model_factory or self._model
        self.catalog_factory = catalog_factory or PlanningToolCatalog.from_local_env
        self.tasks = set()
        self.semaphore = asyncio.Semaphore(2)

    @staticmethod
    def _model():
        from app.config import Settings

        from app.config import ENV_FILE
        settings = Settings.from_env(ENV_FILE)
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY not configured")
        return OpenAIResponsesToolModel(
            settings.openai_api_key, settings.openai_model, 30,
            max_model_requests=settings.planner_model_request_limit or None,
            max_output_tokens=settings.planner_response_token_limit or None,
        )

    async def on_confirmed(self, trip_id):
        if not self.enabled:
            return {"status": "disabled"}
        try:
            return await self.start(trip_id)
        except Exception:  # noqa: BLE001 - isolate planning failure from successful Phase 1 confirmation
            # Collection confirmation remains successful even if planning admission fails.
            await self.event_bus.publish(
                trip_id,
                "planning_status",
                {"status": "failed", "code": "planning_start_failed"},
            )
            return {"status": "failed"}

    async def start(self, trip_id, *, retry=False):
        if not self.enabled:
            raise ValueError("Planning is disabled pending acceptance.")
        state = await asyncio.to_thread(self.repository.get_agent_state, trip_id)
        memory = load_runtime_memory(
            (state.memory_json or state.memory) if state else None
        )
        # Validate before snapshot creation, job insertion, or provider initialization.
        request_from_confirmed_memory(memory, trip_id=trip_id, snapshot_id="pending")
        payload = memory.to_canonical_dict()
        fingerprint = hashlib.sha256(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        snapshot = await asyncio.to_thread(
            self.repository.create_confirmed_trip_snapshot,
            trip_id,
            payload,
            fingerprint,
        )
        request = request_from_confirmed_memory(
            load_runtime_memory(snapshot.payload),
            trip_id=trip_id,
            snapshot_id=str(snapshot.id),
        )
        job, created = await self.store.create_job(trip_id, snapshot.id, retry)
        if created:
            task = asyncio.create_task(self._run(job, request))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return {"run_id": job["id"], "status": job["status"], "created": created}

    async def _run(self, job, request):
        catalog = model = None
        async with self.semaphore:
            try:
                await self.store.update(job["id"], "running", "Starting", 0)
                catalog, model = self.catalog_factory(), self.model_factory()
                journal = CollectionJournal(self.store, request.snapshot_id)
                if catalog.bright_data:
                    catalog.bright_data.journal = journal
                progress = ProgressReporter(
                    job["id"], PlanningProgressSink(self.event_bus, request.trip_id)
                )
                runtime = SixAgentRuntime(model, catalog, progress)
                agents = {name: cls(runtime) for name, cls in AGENT_TEMPLATES.items()}
                highest_progress = 0

                async def save(plan):
                    return await self.store.save_plan(plan, request.snapshot_id)

                async def stage(task, routing_count):
                    nonlocal highest_progress
                    # Stages are estimates; never report 100 before save succeeds.
                    percent = min(
                        90,
                        {
                            "research": 10,
                            "allocation": 30,
                            "accommodation": 45,
                            "transport": 60,
                            "schedule": 80,
                        }[task]
                        + (5 if routing_count > 5 else 0),
                    )
                    percent = highest_progress = max(highest_progress, percent)
                    await self.store.update(job["id"], "running", task, percent)
                    await self.event_bus.publish(
                        request.trip_id,
                        "planning_status",
                        {
                            "run_id": job["id"],
                            "status": "running",
                            "step": task,
                            "progress_percent": percent,
                        },
                    )

                graph = build_graph(
                    GraphHandlers(
                        research=agents["destination_research"],
                        itinerary=agents["itinerary_planning"],
                        accommodation=agents["accommodation"],
                        transport=agents["transport_booking"],
                        supervisor=agents["supervisor"],
                        presentation=agents["response_aggregator"],
                        persist=save,
                        progress=stage,
                    ),
                    checkpointer=PostgresControlCheckpointer(self.store),
                )
                state = await graph.ainvoke(
                    initial_state(request, run_id=job["id"], planning_profile=self.planning_profile),
                    {"configurable": {"thread_id": job["id"]}, "recursion_limit": 24},
                )
                saved = state["persistence"].get("status") == "saved"
                status = "completed" if saved else "failed"
                result = {
                    "plan_id": state["persistence"].get("plan_id"),
                    "plan_status": state["final_plan"].status,
                    "tool_usage": catalog.tool_usage(),
                    "provider_usage": catalog.provider_usage(),
                    "model_usage": runtime.usage,
                }
                await self.store.update(
                    job["id"],
                    status,
                    "Saved" if saved else "Save failed",
                    100 if saved else 95,
                    result=result,
                    failure=None if saved else {"code": "storage_failed"},
                )
                await self.event_bus.publish(
                    request.trip_id,
                    "planning_status",
                    {
                        "run_id": job["id"],
                        "status": status,
                        "progress_percent": 100 if saved else 95,
                        "plan_id": result["plan_id"],
                    },
                )
            except asyncio.CancelledError:
                await self.store.update(
                    job["id"], "interrupted", "Interrupted; explicit retry required", 0
                )
                raise
            except Exception:  # noqa: BLE001 - background job boundary, expose safe failure code
                await self.store.update(
                    job["id"],
                    "failed",
                    "Planning failed",
                    0,
                    failure={"code": "planning_failed"},
                )
                await self.event_bus.publish(
                    request.trip_id,
                    "planning_status",
                    {"run_id": job["id"], "status": "failed"},
                )
            finally:
                if catalog:
                    await catalog.close()
                if model and hasattr(model, "close"):
                    await model.close()

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
