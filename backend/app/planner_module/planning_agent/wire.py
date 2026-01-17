"""Model-facing lists for keyed draft fields; internal planning models stay unchanged."""
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from .contracts import DatedDayDraft, TripDraft, UpdateEnvelope, VisitAllowance
from .protocol import ModelReply, strict_object_schema
from .agent_outputs import PresentationResult, ReviewResult
from .stages import AgentRole

Key = TypeVar("Key")
Value = TypeVar("Value")


class Entry(BaseModel, Generic[Key, Value]):
    model_config = ConfigDict(extra="forbid")
    key: Key
    value: Value


def keyed(entries):
    result = {}
    for entry in entries:
        if entry.key in result:
            raise ValueError("Duplicate keyed draft entry")
        result[entry.key] = entry.value
    return result


class WireDay(DatedDayDraft):
    visit_allowances: list[Entry[str, VisitAllowance]] = Field(default_factory=list)


class WireDraft(TripDraft):
    nights_by_base: list[Entry[str, int]] = Field(default_factory=list)
    stay_area_by_night: list[Entry[int, str]] = Field(default_factory=list)
    alternatives: list[Entry[str, list[str]]] = Field(default_factory=list)
    days: list[WireDay] = Field(default_factory=list)

    @classmethod
    def from_internal(cls, draft: TripDraft):
        value = draft.model_dump(mode="json")
        for name in ("nights_by_base", "stay_area_by_night", "alternatives"):
            value[name] = [{"key": key, "value": item} for key, item in value[name].items()]
        for day in value["days"]:
            day["visit_allowances"] = [{"key": key, "value": item}
                                       for key, item in day["visit_allowances"].items()]
        return cls.model_validate(value)

    def to_internal(self):
        value = self.model_dump(mode="python")
        for name in ("nights_by_base", "stay_area_by_night", "alternatives"):
            value[name] = keyed(getattr(self, name))
        for raw, day in zip(value["days"], self.days):
            raw["visit_allowances"] = keyed(day.visit_allowances)
        return TripDraft.model_validate(value)


class WireUpdate(UpdateEnvelope):
    draft: WireDraft

    def to_internal(self):
        return UpdateEnvelope.model_validate({**self.model_dump(mode="python"),
                                               "draft": self.draft.to_internal()})


class WireReview(ReviewResult):
    corrected_candidate: WireUpdate | None = None

    def to_internal(self):
        return ReviewResult.model_validate({**self.model_dump(mode="python"),
            "corrected_candidate": (self.corrected_candidate.to_internal()
                                     if self.corrected_candidate else None)})


class WireReply(ModelReply):
    update: WireUpdate | None = None
    review: WireReview | None = None

    def to_internal(self):
        return ModelReply.model_validate({**self.model_dump(mode="python"),
            "update": self.update.to_internal() if self.update else None,
            "review": self.review.to_internal() if self.review else None})


def update_schema():
    return strict_object_schema(WireUpdate.model_json_schema())


def result_schema(agent: AgentRole):
    model = {AgentRole.PLANNING: WireUpdate, AgentRole.REVIEW: WireReview,
             AgentRole.PRESENTATION: PresentationResult}[agent]
    return strict_object_schema(model.model_json_schema())
