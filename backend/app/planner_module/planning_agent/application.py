"""Lazy application binding for the guarded planner runtime nodes."""
import os
from pathlib import Path
from uuid import uuid4

from ..utils.logging import run_id
from .api_backend import PlannerAPIBackend
from .checkpoints import CheckpointConflict, CheckpointSession, PlannerCheckpointStore
from .contracts import fingerprint
from .delivery import DeliveryGate
from .integration import assemble_pipeline_state
from .preparation import prepare_utilities
from .resources import PlannerLimits, ResourceGuard, ResourceLedger
from .route_tool import PlannerRouteTool
from .runtime import GuardedPlannerRuntime
from .web_sources import SourceRegistry
from .web_tools import PlannerWebTools
from .workflow import NODE_NAMES
from .workflow import build_guarded_workflow
from .graph_checkpoints import PlannerGraphSaver
from .constraints import current_validation_policy


def build_application_runner(agent, *, stage_callback=None, store=None, backend=None,
                             identifier=None, owner_scope=None):
    """Checkpoint only the planner subgraph, never research or provider settings."""
    async def run(state):
        from ..utils.logging import observe_node

        current_id = identifier or run_id.get()
        if current_id == "-":
            current_id = uuid4().hex
        identity = dict(run_id=current_id, owner_scope=owner_scope or f"run:{current_id}",
                        input_hash=fingerprint(state["input"]))
        target = store or PlannerCheckpointStore(Path(os.getenv("PLANNER_CHECKPOINT_PATH",
            str(Path(__file__).resolve().parents[3] / "runtime/planner/checkpoints.sqlite3"))))
        try:
            _, saved = target.load(**identity)
        except CheckpointConflict:
            target.create(**identity, state={"input": state["input"].model_dump(mode="json")})
            saved = {}
        artifact = target.load_artifact(**identity) or saved.get("final_artifact")
        if artifact is not None and artifact.get("validation", {}).get("policy_version") != current_validation_policy():
            raise CheckpointConflict(
                "Saved planner output uses an older or missing validation policy. "
                "Start a new planning run with the saved research; the original output is unchanged."
            )
        nodes = build_application_nodes(agent, store=target, backend=backend,
                                        identifier=current_id, owner_scope=identity["owner_scope"])
        observed = {name: observe_node(name, node, stage_callback=stage_callback, include_output=False)
                    for name, node in nodes.items()}
        graph = build_guarded_workflow(observed, checkpointer=PlannerGraphSaver(target, **identity))
        config = {"configurable": {"thread_id": current_id}, "recursion_limit": 100}
        snapshot = await graph.aget_state(config)
        if snapshot.values and not snapshot.next:
            if not snapshot.values.get("planner_persisted") or artifact is None:
                raise CheckpointConflict("Completed graph has no persisted checked planner artifact")
            return snapshot.values
        return await graph.ainvoke(None if snapshot.values else {"input": state["input"]}, config)

    return run


def build_application_nodes(agent, *, store=None, backend=None, identifier=None, owner_scope=None):
    runtime = None

    def restore(state, identity, *, create=False):
        nonlocal runtime
        data = state["input"]
        current_id = identity["run_id"]
        expected_id = identifier or run_id.get()
        if expected_id != "-" and current_id != expected_id:
            raise CheckpointConflict("Planner checkpoint belongs to another run")
        if identity != dict(run_id=current_id, owner_scope=owner_scope or f"run:{current_id}",
                            input_hash=fingerprint(data)):
            raise CheckpointConflict("Planner checkpoint identity does not match input or owner")
        target = store or PlannerCheckpointStore(Path(os.getenv("PLANNER_CHECKPOINT_PATH",
            str(Path(__file__).resolve().parents[3] / "runtime/planner/checkpoints.sqlite3"))))
        try:
            target.load(**identity)
        except CheckpointConflict:
            if not create:
                raise
            # A colliding run with a different input/owner still fails at create.
            target.create(**identity, state={"input": data.model_dump(mode="json")})
        session = CheckpointSession(target, **identity)
        config = getattr(agent, "config", None)
        ledger = (ResourceLedger.start(seconds=config.runtime_seconds, reserve=config.finalization_reserve_seconds,
                  limits=PlannerLimits(model=config.max_llm_calls, repairs=config.max_repair_cycles,
                                       route=config.max_route_calls, search=config.max_search_calls,
                                       extract=config.max_extract_calls,
                                       model_seconds=config.model_timeout_seconds))
                  if config is not None else ResourceLedger.start())
        guard = ResourceGuard(ledger)
        routes = PlannerRouteTool(data, guard, session.save)
        entities = set(routes.resolver.nodes) | {poi.id for poi in data.pois}
        web = PlannerWebTools(api_key=os.getenv("TAVILY_API_KEY", ""),
            registry=SourceRegistry(current_id, entities), guard=guard, gaps={})
        utilities = prepare_utilities(data, routes=routes, web=web)
        gate = DeliveryGate(utilities)
        runtime = GuardedPlannerRuntime(data=data, session=session,
            backend=backend if backend is not None else PlannerAPIBackend(agent.client, agent.config),
            utilities=utilities, validate_details=gate.validate, finalize=gate.finalize, resource_ledger=ledger)

    async def prepare(state):
        current_id = identifier or run_id.get()
        if current_id == "-":
            current_id = uuid4().hex
        identity = dict(run_id=current_id, owner_scope=owner_scope or f"run:{current_id}",
                        input_hash=fingerprint(state["input"]))
        restore(state, identity, create=True)
        active = runtime
        return {**await active.prepare_state(state), "planner_identity": identity}

    def bind(name):
        async def invoke(state):
            identity = state.get("planner_identity")
            if not isinstance(identity, dict) or set(identity) != {"run_id", "owner_scope", "input_hash"}:
                raise CheckpointConflict("Planner node requires its prepared checkpoint identity")
            rehydrated = runtime is None
            if rehydrated:
                restore(state, identity)
            active = runtime
            if rehydrated:
                await active.prepare_state(state)
            if identity != active.session.identity or fingerprint(state["input"]) != identity["input_hash"]:
                raise CheckpointConflict("Planner node cannot reuse another run's runtime")
            # Validate the existing application output before freezing this run.
            pipeline_output = (assemble_pipeline_state(active.artifact, active.data, active.utilities.step_facts)
                               if name == "persist_and_present" else None)
            changes = await getattr(active, "work_guard" if name == "guard" else name)(state)
            if pipeline_output is not None:
                changes.update(pipeline_output)
            return changes
        return invoke

    return {name: prepare if name == "prepare_state" else bind(name) for name in NODE_NAMES}
