"""Small, UI-independent contracts for planning progress."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)


class PlanningStage(StrEnum):
    STARTING = "starting"
    CHECKING_DESTINATION = "checking_destination"
    FINDING_PLACES = "finding_places"
    FINDING_STAYS = "finding_stays"
    CHECKING_DETAILS = "checking_details"
    BUILDING_ITINERARY = "building_itinerary"
    CHECKING_ROUTES = "checking_routes"
    ESTIMATING_COSTS = "estimating_costs"
    REVIEWING_PLAN = "reviewing_plan"
    IMPROVING_PLAN = "improving_plan"
    FINALIZING = "finalizing"
    SAVING = "saving"

    @property
    def message(self) -> str:
        """Return the one approved user-facing message for this stage."""
        return self.value.replace("_", " ").capitalize()


class ProgressEventKind(StrEnum):
    STAGE_STARTED = "stage_started"
    HEARTBEAT = "heartbeat"
    WAITING = "waiting"
    RETRYING = "retrying"
    STAGE_COMPLETED = "stage_completed"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"


class ProgressDetail(StrEnum):
    STILL_WORKING = "Still working"
    CHECKING_ANOTHER_SOURCE = "Checking another source"
    WAITING_FOR_TRAVEL_DATA = "Waiting for travel data"
    RECHECKING_THIS_STEP = "Rechecking this step"
    STEP_TAKING_LONGER = "This step is taking longer"


class ProgressEvent(BaseModel):
    """One safe event that may be returned directly to a frontend."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=120)
    sequence: int = Field(ge=1)
    stage: PlanningStage
    kind: ProgressEventKind
    detail: ProgressDetail | None = None
    occurred_at: datetime
    progress_percent: int = Field(default=0, ge=0, le=100)
    node: str | None = None

    @model_validator(mode="before")
    @classmethod
    def ignore_serialized_message(cls, value: Any) -> Any:
        """Always derive the message from the trusted stage enum."""
        if isinstance(value, dict):
            value = dict(value)
            value.pop("message", None)
        return value

    @computed_field  # type: ignore[prop-decorator]  # Supported by Pydantic.
    @property
    def message(self) -> str:
        return self.stage.message

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value
