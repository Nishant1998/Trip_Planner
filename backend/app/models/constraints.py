"""Explicit constraints and the finite V3 relaxation policy."""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from enum import IntEnum, StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ConstraintStrength(StrEnum):
    """How the planner must treat one requirement."""

    IMMUTABLE = "immutable"
    RELAXABLE = "relaxable"
    PREFERENCE = "preference"


class RelaxationStep(IntEnum):
    """Ordered, bounded relaxation ladder. Lower values are tried first."""

    REMOVE_FILLER = 1
    DROP_OPTIONAL_POI = 2
    USE_MINIMUM_VISIT_DURATION = 3
    MOVE_OPTIONAL_ACTIVITY = 4
    CHANGE_BASE = 5
    USE_FASTER_ALLOWED_MODE = 6
    EXTEND_DAY_END = 7
    ALLOW_BUDGET_TOLERANCE = 8
    DROP_LOW_PRIORITY_PREFERENCE = 9
    # Last-resort partial planning. It is disabled unless the caller explicitly
    # opts in through RelaxationPolicy.allow_partial_plan.
    OMIT_UNFITTABLE_STOP = 10


class ImmutableTripConstraints(BaseModel):
    """Values that no agent or relaxation step may change."""

    origin: str
    destination_request: list[str]
    required_stops: list[str]
    start_date: date
    end_date: date
    traveller_count: int = Field(ge=1)
    trip_budget: Decimal = Field(ge=0)
    day_start_time: time
    day_end_time: time
    end_location: str | None = None
    allowed_long_distance_modes: list[str] = Field(default_factory=list)
    allowed_local_modes: list[str] = Field(default_factory=list)
    must_see: list[str] = Field(default_factory=list)
    dietary_restrictions: list[str] = Field(default_factory=list)
    accessibility_needs: list[str] = Field(default_factory=list)
    safety_avoidances: list[str] = Field(default_factory=list)


class PlanningLimits(BaseModel):
    """Initial limits used by the feasibility engine."""

    day_start_time: time
    day_end_time: time
    budget: Decimal = Field(ge=0)
    currency_code: str
    allowed_long_distance_modes: list[str] = Field(default_factory=list)
    allowed_local_modes: list[str] = Field(default_factory=list)
    must_see: list[str] = Field(default_factory=list)


class RelaxationPolicy(BaseModel):
    """Project policy, never invented by the model for a single run."""

    enabled_steps: list[RelaxationStep] = Field(
        default_factory=lambda: [
            RelaxationStep.REMOVE_FILLER,
            RelaxationStep.DROP_OPTIONAL_POI,
            RelaxationStep.USE_MINIMUM_VISIT_DURATION,
            RelaxationStep.MOVE_OPTIONAL_ACTIVITY,
            RelaxationStep.CHANGE_BASE,
            RelaxationStep.USE_FASTER_ALLOWED_MODE,
            RelaxationStep.DROP_LOW_PRIORITY_PREFERENCE,
            # A useful disclosed draft is better than an empty failure screen
            # when the confirmed dates cannot accommodate every destination.
            RelaxationStep.OMIT_UNFITTABLE_STOP,
        ]
    )
    # Confirmed daily hours and budget are not extended by an automatic repair.
    maximum_day_end_extension_minutes: int = Field(default=0, ge=0, le=0)
    maximum_budget_overrun_percent: Decimal = Field(default=Decimal("0"), ge=0, le=0)
    may_reorder_unfixed_stops: bool = True
    may_change_hotel_base: bool = True
    may_drop_optional_pois: bool = True
    may_drop_must_see: bool = False
    allow_partial_plan: bool = True

    def permits(self, step: RelaxationStep) -> bool:
        if step == RelaxationStep.OMIT_UNFITTABLE_STOP and not self.allow_partial_plan:
            return False
        return step in self.enabled_steps


class ConstraintBundle(BaseModel):
    """Complete constraint input to candidate generation."""

    immutable: ImmutableTripConstraints
    limits: PlanningLimits
    policy: RelaxationPolicy
    trip_context_notes: list[str] = Field(default_factory=list)
    field_sources: dict[str, str] = Field(default_factory=dict)


class AppliedRelaxation(BaseModel):
    """Machine-readable disclosure of one change needed for feasibility."""

    step: RelaxationStep
    code: str
    reason: str
    affected_ids: list[str] = Field(default_factory=list)
    original_value: Any = None
    relaxed_value: Any = None


class RelaxationAttempt(BaseModel):
    """Saved diagnostic for one bounded ladder attempt."""

    step: RelaxationStep | None = None
    feasible: bool
    issue_codes: list[str] = Field(default_factory=list)
    applied: list[AppliedRelaxation] = Field(default_factory=list)
