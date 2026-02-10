"""LLM-owned choices that are safe to pass to deterministic planning."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class NoteDirective(BaseModel):
    """One open-ended note converted to a bounded planning directive."""

    note_index: int = Field(ge=0)
    category: Literal[
        "pace",
        "time",
        "meal",
        "lodging",
        "transport",
        "poi",
        "accessibility",
        "safety",
        "other",
    ]
    importance: Literal["required", "preferred", "advisory"]
    instruction: str = Field(min_length=1, max_length=240)


class NoteAcknowledgement(BaseModel):
    """A note that was read but does not create a new planning instruction."""

    note_index: int = Field(ge=0)
    disposition: Literal["already_covered", "not_actionable", "cannot_apply"]
    reason: str = Field(min_length=1, max_length=240)


class RankedPlace(BaseModel):
    """A preference score for an exact evidence ID, not a new place claim."""

    place_id: str
    score: int = Field(ge=0, le=100)
    reason: str = Field(min_length=1, max_length=200)


class PlannerIntent(BaseModel):
    """The Planner Agent's only creative output before calling the solver."""

    ranked_places: list[RankedPlace]
    note_directives: list[NoteDirective] = Field(default_factory=list)
    note_acknowledgements: list[NoteAcknowledgement] = Field(default_factory=list)
    preferred_long_distance_modes: list[str] = Field(default_factory=list)
    preferred_local_modes: list[str] = Field(default_factory=list)
