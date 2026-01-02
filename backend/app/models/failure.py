"""Safe public failures and useful sanitized developer diagnostics."""

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

from app.models.progress import PlanningStage

PUBLIC_FAILURE_MESSAGE = "We couldn't safely complete this trip plan. Please try again."


class FailureCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    DESTINATION_UNRESOLVED = "destination_unresolved"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    PLACE_DETAILS_UNVERIFIED = "place_details_unverified"
    ROUTE_DATA_UNAVAILABLE = "route_data_unavailable"
    BUDGET_CANNOT_BE_CHECKED = "budget_cannot_be_checked"
    MODEL_OUTPUT_INVALID = "model_output_invalid"
    FINAL_REVIEW_BLOCKED = "final_review_blocked"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PLANNING_TIMEOUT = "planning_timeout"
    INTERNAL_ERROR = "internal_error"


_REASONS: dict[FailureCode, str] = {
    FailureCode.INVALID_INPUT: "The trip information could not be read correctly.",
    FailureCode.DESTINATION_UNRESOLVED: "We could not verify the destination clearly.",
    FailureCode.EVIDENCE_INSUFFICIENT: (
        "We could not find enough reliable place information for these dates."
    ),
    FailureCode.PLACE_DETAILS_UNVERIFIED: (
        "We could not verify important details for one or more places."
    ),
    FailureCode.ROUTE_DATA_UNAVAILABLE: (
        "We could not calculate a reliable route for part of the trip."
    ),
    FailureCode.BUDGET_CANNOT_BE_CHECKED: (
        "We could not build a reliable cost estimate for the trip."
    ),
    FailureCode.MODEL_OUTPUT_INVALID: (
        "The itinerary could not be prepared in the required format."
    ),
    FailureCode.FINAL_REVIEW_BLOCKED: (
        "The plan still had a timing, route, or evidence-consistency problem after review."
    ),
    FailureCode.PROVIDER_UNAVAILABLE: (
        "A required planning service was temporarily unavailable."
    ),
    FailureCode.PLANNING_TIMEOUT: (
        "Planning did not finish within the configured time limit."
    ),
    FailureCode.INTERNAL_ERROR: "An unexpected planning problem occurred.",
}

_NOT_RETRYABLE = {FailureCode.INVALID_INPUT, FailureCode.DESTINATION_UNRESOLVED}


class PublicFailure(BaseModel):
    """Failure information safe enough to return to a traveller."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=120)
    code: FailureCode

    @model_validator(mode="before")
    @classmethod
    def ignore_serialized_display_fields(cls, value: Any) -> Any:
        """Recompute display text instead of trusting values sent over HTTP."""
        if isinstance(value, dict):
            value = dict(value)
            for field_name in ("message", "reason", "retryable"):
                value.pop(field_name, None)
        return value

    @computed_field  # type: ignore[prop-decorator]  # Supported by Pydantic.
    @property
    def message(self) -> str:
        return PUBLIC_FAILURE_MESSAGE

    @computed_field  # type: ignore[prop-decorator]  # Supported by Pydantic.
    @property
    def reason(self) -> str:
        return _REASONS[self.code]

    @computed_field  # type: ignore[prop-decorator]  # Supported by Pydantic.
    @property
    def retryable(self) -> bool:
        return self.code not in _NOT_RETRYABLE


class FailureDiagnostic(BaseModel):
    """Sanitized failure information stored for developers, never rendered directly."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=120)
    error_id: str = Field(min_length=1, max_length=120)
    code: FailureCode
    failed_stage: PlanningStage | None = None
    component: str | None = Field(default=None, max_length=160)
    operation: str | None = Field(default=None, max_length=160)
    attempt: int = Field(default=1, ge=1)
    exception_type: str = Field(min_length=1, max_length=160)
    sanitized_message: str = Field(min_length=1, max_length=2000)
    occurred_at: datetime

    @computed_field  # type: ignore[prop-decorator]  # Supported by Pydantic.
    @property
    def retryable(self) -> bool:
        return self.code not in _NOT_RETRYABLE

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value
