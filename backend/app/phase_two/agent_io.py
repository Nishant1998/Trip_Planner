"""Small model outputs: factual records come from tool evidence, not the LLM."""

from datetime import datetime
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    period: Literal["morning", "midday", "afternoon", "evening", "flexible"] | None = None
    place_id: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    visit_minutes: int | None = Field(default=None, ge=0)
    duration_basis: Literal["provider", "planning_allowance", "unknown"] = "unknown"


class DayProposal(Output):
    day: int = Field(ge=1)
    base_location: str | None = None
    title: str = ""
    summary: str = ""
    events: list[EventProposal] = Field(default_factory=list)


class ItineraryOutput(Output):
    days: list[DayProposal] = Field(default_factory=list)
    stays: list[StayRequest] = Field(default_factory=list)
    journeys: list[JourneyRequest] = Field(default_factory=list)
    route_requirements: list[RouteRequirement] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class EventCopy(Output):
    event_id: str
    description: str = Field(min_length=1, max_length=800)


class DayCopy(Output):
    day: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=600)
    events: list[EventCopy] = Field(default_factory=list)


class PresentationOutput(Output):
    title: str
    overview: str
    days: list[DayCopy] = Field(default_factory=list)
    local_tips: list[str] = Field(default_factory=list, max_length=6)
    packing_list: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def no_service_confirmation(self) -> "PresentationOutput":
        # The writer has no booking tool. Prefer requests to confirm, even in
        # conditional wording, over phrases readers can mistake for reservations.
        text = [self.title, self.overview, *self.local_tips, *self.packing_list]
        for day in self.days:
            text.extend([day.title, day.summary, *(event.description for event in day.events)])
        pattern = r"\bconfirmed\s+(?:vegetarian\s+)?(?:meals?|lunch|dinner|hotels?|rooms?|accommodation|luggage|storage|reservations?|tickets?)\b"
        if any(re.search(pattern, item, re.IGNORECASE) for item in text):
            raise ValueError("Unsupported service confirmation; write a request to confirm instead")
        return self
