"""Run real Riya conversations and planning from the terminal.

python -m scripts.generate_sample_trips --live --case jaipur-heritage
Generated packets are private review material until approved for publication.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from uuid import uuid4

from app.collection.agents.registry import AgentRegistry
from app.collection.confirmation_gate import has_current_confirmation_summary
from app.collection.runtime import CollectionRuntimeService
from app.config import Settings
from app.db.factory import build_trip_repository
from app.models.enums import TripStatus
from app.models.state import load_runtime_memory
from app.itinerary.service import PlanningService
from app.services.events import TripEventBus
from psycopg.types.json import Json
from scripts.sample_export import export_sample, write_json, write_manifest

BACKEND = Path(__file__).resolve().parents[1]


def load_memory(repository, trip_id):
    state = repository.get_agent_state(trip_id)
    return load_runtime_memory(state.memory_json or state.memory)


async def show_progress(bus, trip_id):
    queue = await bus.subscribe(trip_id)
    last_text = None
    try:
        while True:
            raw = await queue.get()
            name, data = raw.split("\n", 1)
            payload = json.loads(data.removeprefix("data: ").strip())
            if name in {
                "event: status",
                "event: planning_progress",
                "event: planning_status",
            }:
                text = (
                    payload.get("message")
                    or payload.get("detail")
                    or payload.get("status")
                )
                if text and text != last_text:
                    print(f"  status: {text}", flush=True)
                    last_text = text
    finally:
        await bus.unsubscribe(trip_id, queue)


async def send(runtime, repository, trip_id, user_id, text):
    print(f"\nYOU: {text}", flush=True)
    before = len(repository.list_messages(trip_id))
    await runtime.submit_user_message(
        trip_id, text, user_id=user_id, client_message_id=str(uuid4())
    )
    async with asyncio.timeout(180):
        while runtime.is_processing(trip_id):
            await asyncio.sleep(0.5)
    messages = repository.list_messages(trip_id)[before:]
    for message in messages:
        if message.role == "assistant":
            print(f"RIYA: {message.content}", flush=True)
            if message.meta.get("branch") == "error":
                raise RuntimeError(
                    f"Collection failed: {message.meta.get('error_type')}"
                )


def create_sample_account(repository, sample_id):
    marker = uuid4().hex[:10]
    user_id = repository.create_user(
        email=f"{sample_id}.{marker}@samples.invalid",
        picture=None,
        username=f"demo_{marker}",
        auth_provider="sample",
        auth_provider_id=marker,
    )
    repository.update_user_profile(user_id, name="Demo traveler")
    with repository._connect() as conn:
        conn.execute(
            "UPDATE users SET preferences=%s WHERE id=%s",
            (Json({"demo_access": "full"}), user_id),
        )
    return user_id


async def generate_case(
    scenario, repository, output, *, keep_trips=False, resume=False
):
    folder = output / scenario["id"]
    if (folder / "run.json").exists() and not resume:
        raise ValueError(
            "A run already exists; use --resume or choose a new output directory"
        )
    if resume:
        previous = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        user_id = previous["owner_id"]
        account = repository.get_user(user_id)
        trip = repository.get_trip(previous["trip_id"])
        if (
            not account
            or account.auth_provider != "sample"
            or not trip
            or trip.owner_id != user_id
        ):
            raise ValueError("Resume accepts only an existing isolated sample trip")
    else:
        user_id = create_sample_account(repository, scenario["id"])
        trip = repository.create_trip(user_id)
    bus = TripEventBus()
    agents = AgentRegistry.build_default()
    planning = PlanningService(
        repository, bus, enabled=True, planning_profile="daywise"
    )
    runtime = CollectionRuntimeService(
        repository, bus, agents, on_trip_confirmed=planning.on_confirmed
    )
    progress = asyncio.create_task(show_progress(bus, trip.id))
    private = {"sample_id": scenario["id"], "trip_id": trip.id, "owner_id": user_id}
    write_json(folder / "run.json", private)
    try:
        if not resume:
            await runtime.bootstrap_trip(trip.id)
            for step in ("opening", "preferences", "correction"):
                await send(runtime, repository, trip.id, user_id, scenario[step])
        else:
            await send(runtime, repository, trip.id, user_id, scenario["final_brief"])
        for attempt in range(3):
            memory = load_memory(repository, trip.id)
            if (
                memory.metadata.status == TripStatus.AWAITING_CONFIRMATION
                and has_current_confirmation_summary(memory)
            ):
                break
            if attempt < 2:
                await send(
                    runtime,
                    repository,
                    trip.id,
                    user_id,
                    scenario["final_brief"]
                    if attempt == 0
                    else "Please show the saved trip requirements for my approval now. Do not start planning yet.",
                )
        else:
            raise RuntimeError(
                "Collection did not reach confirmation within the turn budget"
            )
        await send(
            runtime,
            repository,
            trip.id,
            user_id,
            "Yes, the latest summary is correct. Start planning.",
        )
        async with asyncio.timeout(900):
            while True:
                job = await planning.store.status(trip.id)
                if job and job["status"] not in {"queued", "running"}:
                    break
                if not job and not planning.tasks:
                    raise RuntimeError("Confirmation did not start a planning job")
                await asyncio.sleep(2)
        if job["status"] != "completed":
            raise RuntimeError(
                f"Planning ended with {job['status']}: {job.get('failure')}"
            )
        saved = repository.get_active_trip_plan(trip.id)
        if saved is None:
            raise RuntimeError("Completed job has no saved itinerary")
        plan = planning.display_plans.get(saved.plan_json["plan_id"])
        if plan is None:
            raise RuntimeError("Fresh display content expired before export")
        memory = load_memory(repository, trip.id)
        usage = {
            "collection": repository.get_trip_llm_usage(trip.id).model_dump(),
            "planning": (job.get("result") or {}).get("model_usage", {}),
            "providers": (job.get("result") or {}).get("provider_usage", {}),
        }
        report = export_sample(
            folder, scenario, repository.list_messages(trip.id), memory, plan, usage
        )
        # Read back the export before deleting the disposable trip.
        packet = json.loads((folder / "sample.json").read_text(encoding="utf-8"))
        assert (
            packet["id"] == scenario["id"]
            and packet["conversation"]
            and packet["plan"]["days"]
        )
        if not keep_trips:
            assert repository.delete_trip(trip.id, user_id)
            with repository._connect() as conn:
                conn.execute(
                    "DELETE FROM users WHERE id=%s AND auth_provider='sample' AND NOT EXISTS (SELECT 1 FROM trips WHERE owner_id=%s)",
                    (user_id, user_id),
                )
            private["temporary_records_removed"] = True
        private.update(status="completed", review=report)
        print(
            f"\nCOMPLETED {scenario['id']}: {report['plan_status']}; {len(report['blocking_issues'])} blocking issues",
            flush=True,
        )
        return private
    except Exception as exc:
        private.update(status="failed", error=str(exc))
        print(f"\nFAILED {scenario['id']}: {exc}", flush=True)
        return private
    finally:
        await planning.close()
        await runtime.close()
        progress.cancel()
        await asyncio.gather(progress, return_exceptions=True)
        write_json(folder / "run.json", private)


async def main(args):
    scenarios = json.loads(
        (BACKEND / "samples/scenarios.json").read_text(encoding="utf-8")
    )
    selected = [case for case in scenarios if not args.case or case["id"] == args.case]
    if not selected:
        raise SystemExit("Unknown sample ID")
    if not args.live:
        for case in selected:
            print(f"{case['id']}: {case['title']} — {', '.join(case['features'])}")
        print("Use --live to make real model/provider calls.")
        return
    settings = Settings.from_env()
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is required; no model fallback is used.")
    logging.basicConfig(level=logging.ERROR)
    repository = build_trip_repository()
    # Read-only preflight; never create or migrate a schema from this command.
    with repository._connect() as conn:
        for table in (
            "planning_jobs",
            "phase_two_control_checkpoints",
            "phase_two_collections",
        ):
            if not conn.execute("SELECT to_regclass(%s) AS name", (table,)).fetchone()[
                "name"
            ]:
                raise SystemExit(
                    f"Required table missing: {table}; migrations were not applied."
                )
    output = Path(args.output).resolve()
    results = []
    for case in selected:
        completed = output / case["id"] / "run.json"
        if completed.exists():
            previous = json.loads(completed.read_text(encoding="utf-8"))
            if (
                previous.get("status") == "completed"
                and (completed.parent / "sample.json").exists()
            ):
                print(
                    f"Already generated {case['id']}; keeping its export without new API calls."
                )
                results.append(previous)
                continue
        results.append(
            await generate_case(
                case, repository, output, keep_trips=args.keep_trips, resume=args.resume
            )
        )
        write_manifest(output, scenarios)
    if any(result["status"] != "completed" for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--case")
    parser.add_argument("--output", default=str(BACKEND / "samples/generated"))
    parser.add_argument("--keep-trips", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue an isolated sample conversation instead of creating another trip",
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main(parser.parse_args()))
