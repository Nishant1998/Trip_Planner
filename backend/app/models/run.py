"""Typed API contracts for an asynchronous planning run."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from app.models.failure import PublicFailure
from app.models.progress import ProgressEvent
from typing import Any


class PlanningRunState(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class PlanningRunAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    state: PlanningRunState
    poll_url: str

    @model_validator(mode="after")
    def require_running_state(self) -> Self:
        if self.state is not PlanningRunState.RUNNING:
            raise ValueError("an accepted run must be running")
        return self


class PlanningRunSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    state: PlanningRunState
    started_at: datetime
    finished_at: datetime | None = None
    latest_event: ProgressEvent | None = None
    # The durable runner can carry either the historic TripPlan or the final
    # autonomous pipeline result during the compatibility transition.
    result: Any | None = None
    failure: PublicFailure | None = None

    @model_validator(mode="after")
    def require_state_payload(self) -> Self:
        if self.state is PlanningRunState.RUNNING:
            if self.finished_at or self.result or self.failure:
                raise ValueError("a running snapshot cannot contain a terminal payload")
        elif self.state is PlanningRunState.COMPLETED:
            if (
                self.finished_at is None
                or self.result is None
                or self.failure is not None
            ):
                raise ValueError("a completed snapshot requires only a result")
        elif (
            self.finished_at is None or self.failure is None or self.result is not None
        ):
            raise ValueError("a failed snapshot requires only a public failure")
        return self
