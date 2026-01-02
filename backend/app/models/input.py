"""Structural contract for Riya's canonical TripMemory JSON.

Riya owns business validation. This model validates the storage shape without
repeating Riya's conversational requirement rules.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ValueRecord(BaseModel):
    """Metadata value in canonical storage form."""

    model_config = ConfigDict(extra="allow")

    value: Any = None


class SourcedValueRecord(ValueRecord):
    """Collected value together with Riya's provenance."""

    source: str | None = None


class DestinationSanityInput(BaseModel):
    """Destination evidence already produced by Riya."""

    model_config = ConfigDict(extra="allow")

    destination_verified: bool = False
    resolved_city_names: list[str] = Field(default_factory=list)
    destination_intelligence: dict[str, Any] | None = None


class InternalTripMemoryInput(BaseModel):
    """Internal fields that may accompany a canonical handoff."""

    model_config = ConfigDict(extra="allow")

    sanity_state: DestinationSanityInput = Field(default_factory=DestinationSanityInput)
    trip_context_notes: list[str] = Field(default_factory=list)
    traveler_context: dict[str, str] = Field(default_factory=dict)
    data_confidence: str | None = None


class TripMemoryInput(BaseModel):
    """The canonical object produced by ``TripMemory.to_canonical_dict``."""

    model_config = ConfigDict(extra="allow")

    schema_version: Literal["trip_memory.v1"] = "trip_memory.v1"
    metadata: dict[str, ValueRecord]
    needed: dict[str, SourcedValueRecord]
    optional: dict[str, SourcedValueRecord] = Field(default_factory=dict)
    internal: InternalTripMemoryInput = Field(default_factory=InternalTripMemoryInput)
