"""Small model outputs: factual records come from tool evidence, not the LLM."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .contracts import JourneyRequest, RouteRequirement, StayRequest


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoutingDecision(Output):
    task: Literal[
        "research", "allocation", "accommodation", "transport", "schedule", "finish"
    ]
    reason: str


class PlaceChoice(Output):
    place_id: str
    destination: str
    reason: str


class ResearchOutput(Output):
    choices: list[PlaceChoice] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Selection(Output):
    request_id: str
    option_id: str | None = None
    alternative_ids: list[str] = Field(default_factory=list)
    reason: str


class OptionsOutput(Output):
    selections: list[Selection] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class EventProposal(Output):
    reason: str = ""
    id: str
    kind: Literal[
        "activity", "travel", "meal", "rest", "check_in", "check_out", "unallocated"
    ]
    title: str
    place_id: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    visit_minutes: int | None = Field(default=None, ge=0)
    duration_basis: Literal["provider", "planning_allowance", "unknown"] = "unknown"


class DayProposal(Output):
    day: int = Field(ge=1)
    base_location: str | None = None
    events: list[EventProposal] = Field(default_factory=list)


class ItineraryOutput(Output):
    days: list[DayProposal] = Field(default_factory=list)
    stays: list[StayRequest] = Field(default_factory=list)
    journeys: list[JourneyRequest] = Field(default_factory=list)
    route_requirements: list[RouteRequirement] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PresentationOutput(Output):
    title: str
    overview: str
