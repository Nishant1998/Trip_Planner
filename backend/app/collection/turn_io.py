"""Turn-level repository and publishing helpers for the orchestrator."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.db.repository import TripRepository
from app.db.tables import TripRow
from app.models.api import ExtractionOutput
from app.models.enums import TripStatus
from app.models.memory import TripMemory
from app.models.state import load_runtime_memory, serialize_runtime_memory
from app.services.traveler_context import build_traveler_context


class Publisher(Protocol):
    """Events the collection flow may publish to its UI transport."""

    async def publish_status(self, status: str, message: str | None = None) -> None: ...
    async def publish_state_change(self, old_status: str, new_status: str) -> None: ...
    async def publish_trace(
        self,
        block: str,
        stage: str,
        *,
        input_data: Any = None,
        output_data: Any = None,
        message: str = "",
    ) -> None: ...
    async def publish_stream_text(self, text: str) -> None: ...
    async def publish_stream_chunk(self, chunk: str) -> None: ...
    async def publish_stream_end(self) -> None: ...
    async def publish_suggestions(self, suggestions: list[str]) -> None: ...


@dataclass(slots=True)
class TurnOutcome:
    """Structured result of one collection turn."""

    response_text: str
    status: TripStatus
    branch: str
    checkpoint_payload: dict | None = None
    checkpoint_type: str | None = None
    extracted_fields: list[str] = field(default_factory=list)
    is_correction: bool = False
    sanity_conflicts: list[str] = field(default_factory=list)
    rule_conflicts: list[str] = field(default_factory=list)
    defaults_applied: list[str] = field(default_factory=list)
    error_message: str | None = None
    response_streamed: bool = False
    suggestions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TurnContext:
    chat_history: str
    compressed_profile: str
    traveler_context: dict[str, str]


@dataclass(slots=True)
class ExtractionAttempt:
    output: ExtractionOutput
    retries_exhausted: bool = False


def load_trip_memory(
    repository: TripRepository,
    trip_id: int,
) -> tuple[TripRow, TripMemory]:
    """Load one trip row together with its parsed memory object."""

    trip = repository.get_trip(trip_id)
    if trip is None:
        raise ValueError(f"Trip {trip_id} not found")
    return trip, load_memory_from_trip(trip)


def load_memory_from_trip(trip: TripRow) -> TripMemory:
    """Parse the serialized trip memory from one stored trip row."""

    return load_runtime_memory(trip.agent_state)


def build_turn_context(
    repository: TripRepository,
    trip: TripRow,
    memory: TripMemory,
) -> TurnContext:
    """Build reusable per-turn context from stored messages and profile data."""

    messages = repository.get_recent_messages(trip.id, limit=20)
    chat_history = "\n".join(f"{item.role}: {item.content}" for item in messages)
    profile = repository.get_user_profile_obj(trip.owner_id)
    user = repository.get_user(trip.owner_id)
    compressed_profile = profile.compressed_text if profile else ""
    return TurnContext(
        chat_history=chat_history,
        compressed_profile=compressed_profile,
        traveler_context=build_traveler_context(user),
    )


async def publish_live_status(
    publisher: Publisher | None,
    status: str,
    message: str,
) -> None:
    """Publish one live status message when a publisher is available."""

    if publisher is None:
        return
    await publisher.publish_status(status, message)


async def finalize_turn(
    repository: TripRepository,
    trip: TripRow,
    memory: TripMemory,
    previous_status: TripStatus,
    publisher: Publisher | None,
    outcome: TurnOutcome,
    *,
    clear_checkpoint: bool = False,
) -> TurnOutcome:
    """Persist one turn outcome and publish any state change."""

    from app.collection.suggestions import suggestions_for_turn

    outcome.suggestions = suggestions_for_turn(memory, outcome)

    repository.save_trip_state(trip.id, serialize_runtime_memory(memory))

    if clear_checkpoint:
        repository.clear_pending_checkpoints(trip.id)
    elif outcome.checkpoint_payload is not None and outcome.checkpoint_type is not None:
        repository.save_checkpoint(
            trip_id=trip.id,
            checkpoint_type=outcome.checkpoint_type,
            payload_json=json.dumps(outcome.checkpoint_payload, ensure_ascii=False),
            is_pending=True,
        )
    elif previous_status != TripStatus.AWAITING_CONFIRMATION:
        repository.clear_pending_checkpoints(trip.id)

    repository.insert_message(
        trip.id,
        "assistant",
        outcome.response_text,
        meta={
            "extracted": outcome.extracted_fields,
            "is_correction": outcome.is_correction,
            "sanity": outcome.sanity_conflicts,
            "conflicts": outcome.rule_conflicts,
            "branch": outcome.branch,
            "defaults_applied": outcome.defaults_applied,
            "suggestions": outcome.suggestions,
        },
    )

    if publisher is not None and previous_status != memory.metadata.status:
        await publisher.publish_state_change(
            previous_status.value,
            memory.metadata.status.value,
        )
    return outcome
