"""Versioned update protocol for the guarded planning workflow."""
from __future__ import annotations

import hashlib
import json
from typing import Literal
from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from .models import PlanningCheckpoint, SemanticDayDraft
from .timeline import TimelineRequest
from .calculation import CostItem
from .claims import EvidenceClaim
from .hard_limits import DailyLimit


class VisitAllowance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minutes: int = Field(gt=0, le=1440)
    basis: Literal["research", "planning_allowance"]
    evidence_ids: list[str] = Field(default_factory=list)
    note: str = Field(min_length=1, max_length=1000)


class DatedDayDraft(SemanticDayDraft):
    model_config = ConfigDict(extra="forbid")
    date: date
    overnight_kind: Literal["stay", "in_transit", "none", "unknown"] = "unknown"
    overnight_anchor_id: str | None = None
    visit_allowances: dict[str, VisitAllowance] = Field(default_factory=dict)
    usable_minutes: int | None = Field(default=None, ge=0, le=1440)
    timeline: TimelineRequest | None = None


class TripDraft(PlanningCheckpoint):
    model_config = ConfigDict(extra="forbid")
    days: list[DatedDayDraft] = Field(default_factory=list)
    costs: list[CostItem] = Field(default_factory=list, max_length=256)
    daily_limits: list[DailyLimit] = Field(default_factory=list, max_length=32,
        description="Optional model interpretations of explicit numeric daily maxima in mandatory_constraints; quote exact value/unit spans. These checks do not certify the rest of the requirement.")


class PhaseIssue(BaseModel):
    code: str
    outcome: Literal["fail", "unknown"]
    message: str
    day_number: int | None = None
    entity_ids: list[str] = Field(default_factory=list)


class PhaseCheck(BaseModel):
    issues: list[PhaseIssue] = Field(default_factory=list)
    can_advance_to_details: bool = False
    # Structural checks alone never certify a publishable detailed itinerary.
    ready: Literal[False] = False


def fingerprint(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class UpdateEnvelope(BaseModel):
    """Output protocol, never an executable seventh utility."""

    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(min_length=1, max_length=128)
    input_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    base_plan_version: int = Field(ge=0)
    expected_evidence_revision: int = Field(ge=0)
    phase: Literal["skeleton", "city_details", "repair"]
    draft: TripDraft
    rationale: list[str] = Field(default_factory=list, max_length=12)
    evidence_claims: list[EvidenceClaim] = Field(default_factory=list, max_length=16)


class VersionedDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_version: int = Field(default=0, ge=0)
    evidence_revision: int = Field(default=0, ge=0)
    phase: Literal["skeleton", "city_details", "repair"] = "skeleton"
    draft: TripDraft = Field(default_factory=TripDraft)
    applied_operations: dict[str, str] = Field(default_factory=dict)

    def apply(self, update: UpdateEnvelope) -> VersionedDraft:
        """Produce a candidate copy; phase acceptance remains the validator's job."""
        if update.input_fingerprint != self.input_fingerprint:
            raise ValueError("Input fingerprint mismatch")
        digest = fingerprint(update)
        previous = self.applied_operations.get(update.operation_id)
        if previous:
            if previous != digest:
                raise ValueError("Operation ID reused with different content")
            return self.model_copy(deep=True)
        if (update.base_plan_version != self.plan_version
                or update.expected_evidence_revision != self.evidence_revision):
            raise ValueError("Stale plan or evidence revision")
        if update.phase != self.phase:
            raise ValueError("Only application checks may advance phase")
        candidate = self.model_copy(deep=True)
        candidate.draft = update.draft.model_copy(deep=True)
        candidate.plan_version += int(fingerprint(self.draft) != fingerprint(candidate.draft))
        candidate.applied_operations[update.operation_id] = digest
        return candidate
