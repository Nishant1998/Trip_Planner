"""Durable control state is separate from ephemeral provider display evidence."""

import asyncio
import copy

from app.planning_tools.contracts import ToolResult, ToolStatus
from langgraph.checkpoint.memory import InMemorySaver

from .runtime import json_value


def durable_plan(plan):
    result = copy.deepcopy(json_value(plan))
    def synthetic(record):
        evidence = record.get("evidence", [])
        return bool(evidence) and all(e.get("provider") == "synthetic_level1" for e in evidence)
    # Conservative default: no Google-derived display fields are kept durably.
    for destination in result.get("destinations", []):
        destination["location"] = None
    for day in result.get("days", []):
        day["daily_distance_m"] = None
        for event in day.get("events", []):
            if synthetic(event):
                continue  # Locally authored fixture evidence has no provider retention restriction.
            if event.get("place_id"):
                event["title"] = "Selected place (refresh details)"
                event["reason"] = "Refresh source details to view the activity explanation."
            for field in (
                "location",
                "address",
                "opening_hours",
                "map_url",
                "travel_from_previous",
            ):
                event[field] = None
            event["image_refs"], event["photos"], event["display_photos"] = [], [], []
            event["evidence"] = []
    # Google transit facts are also not a permanent reusable timetable cache.
    for transport in result.get("transport", []):
        if transport.get("mode") in ("train", "bus") and not synthetic(transport):
            transport["selected"], transport["alternatives"] = None, []
            transport["status"] = "unavailable"
    # LLM narrative can repeat restricted source content; regenerate/display only in-run.
    if "trip_summary" in result:
        result["trip_summary"]["overview"] = ""
    if result.get("route_profiles", {}).get("evidence_provider") != "synthetic_level1":
        result["route_profiles"] = {}
    return result


def control_checkpoint(checkpoint):
    safe = copy.deepcopy(json_value(checkpoint))
    allowed = {"run", "user_preferences", "routing", "agent_status"}
    safe["channel_values"] = {
        key: value
        for key, value in safe.get("channel_values", {}).items()
        if key in allowed
    }
    # No arbitrary model prose, facts or tool responses in durable checkpoints.
    if isinstance(safe["channel_values"].get("routing"), dict):
        safe["channel_values"]["routing"]["reason"] = ""
    return safe


class PostgresControlCheckpointer(InMemorySaver):
    """Full checkpoints in RAM; PostgreSQL stores a control-only projection.

    After process loss the service starts a new run from the confirmed snapshot,
    refreshes ephemeral evidence, and reuses the durable collection journal. It
    deliberately does not replay an arbitrary graph node with missing evidence.
    """

    def __init__(self, store):
        super().__init__()
        self.store = store

    async def aput(self, config, checkpoint, metadata, new_versions):
        result = await super().aput(config, checkpoint, metadata, new_versions)
        await self.store.save_checkpoint(
            config["configurable"]["thread_id"],
            control_checkpoint(checkpoint),
            {k: v for k, v in metadata.items() if k in ("step", "source")},
        )
        return result


class CollectionJournal:
    """Persist before submission, save received snapshot IDs before any polling."""

    def __init__(self, store, snapshot_id):
        self.store, self.snapshot_id = store, snapshot_id
        self.lock = asyncio.Lock()

    async def collect(self, key, submit, resume):
        async with self.lock:
            prior = await self.store.read_collection(self.snapshot_id, key)
            if prior:
                if prior["status"] == "finished":
                    return ToolResult.model_validate(prior["result"])
                if prior.get("token"):
                    result = await resume(prior["token"])
                    await self._finish(key, result, prior["token"])
                    return result
                return self.unknown()
            await self.store.write_collection(
                self.snapshot_id, key, {"status": "submitting"}
            )

            async def received(token):
                await self.store.write_collection(
                    self.snapshot_id, key, {"status": "pending", "token": token}
                )

            try:
                result = await submit(received)
                await self._finish(key, result)
                return result
            except Exception:  # noqa: BLE001 - unknown billing outcome must never be resubmitted
                # Retain submitting/pending row. Never assume a failed await was unbilled.
                return self.unknown()

    async def _finish(self, key, result, token=None):
        if result.continuation_token:
            await self.store.write_collection(
                self.snapshot_id,
                key,
                {"status": "pending", "token": result.continuation_token},
            )
        elif result.status in ("ok", "partial") and result.data is not None:
            await self.store.write_collection(
                self.snapshot_id,
                key,
                {"status": "finished", "result": json_value(result)},
            )
        elif token:
            await self.store.write_collection(
                self.snapshot_id, key, {"status": "pending", "token": token}
            )
        # Unknown/error results leave the submission barrier intact.

    @staticmethod
    def unknown():
        return ToolResult(
            status=ToolStatus.UNAVAILABLE,
            provider="bright_data",
            error_code="submission_unknown",
            warnings=[
                "An earlier submission may have been billed. It will not be submitted again automatically."
            ],
        )
