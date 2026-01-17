"""Separate reviewer decisions and presentation text from authoritative trip facts."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import UpdateEnvelope


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    severity: Literal["critical", "important", "minor"]
    day_numbers: list[int] = Field(default_factory=list, max_length=366)
    entity_ids: list[str] = Field(default_factory=list, max_length=128)
    requirement_ids: list[str] = Field(default_factory=list, max_length=128)
    evidence_ids: list[str] = Field(default_factory=list, max_length=128)
    issue: str = Field(min_length=1, max_length=2000)
    requested_correction: str = Field(min_length=1, max_length=2000)


class CorrectionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finding_id: str = Field(min_length=1, max_length=128)
    status: Literal["addressed", "unresolved"]
    explanation: str = Field(min_length=1, max_length=2000)


class HardConstraintAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    constraint_index: int = Field(ge=0)
    source_text: str = Field(min_length=1, max_length=4000)
    outcome: Literal["supported", "violated", "unknown"]
    basis: Literal["itinerary_decision", "external_evidence"]
    day_numbers: list[int] = Field(min_length=1, max_length=366)
    evidence_ids: list[str] = Field(default_factory=list, max_length=128)
    explanation: str = Field(min_length=1, max_length=2000,
        description="Assess every clause of the exact requirement against these days. Do not infer missing external facts or override Python findings.")


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["no_issues_found", "unresolved"]
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=64)
    corrected_candidate: UpdateEnvelope | None = None
    corrections: list[CorrectionRecord] = Field(default_factory=list, max_length=64)
    hard_constraints: list[HardConstraintAssessment] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def consistent_findings(self):
        ids = [item.id for item in self.findings]
        records = [item.finding_id for item in self.corrections]
        if len(set(ids)) != len(ids) or len(set(records)) != len(records):
            raise ValueError("Duplicate review finding or correction identity")
        indices = [row.constraint_index for row in self.hard_constraints]
        if len(set(indices)) != len(indices):
            raise ValueError("Duplicate hard-constraint assessment")
        if self.outcome != "unresolved" and any(row.outcome != "supported" for row in self.hard_constraints):
            raise ValueError("Unresolved or violated hard constraints require an unresolved review")
        if set(ids) != set(records):
            raise ValueError("Every finding needs exactly one correction record")
        if self.outcome == "no_issues_found":
            if any(item.status == "unresolved" for item in self.corrections):
                raise ValueError("No-issues review cannot retain unresolved corrections")
            if self.findings and self.corrected_candidate is None:
                raise ValueError("Addressed findings require the corrected candidate")
        elif not self.findings:
            raise ValueError("Review outcome requires specific findings")
        if self.outcome == "unresolved" and not any(item.status == "unresolved" for item in self.corrections):
            raise ValueError("Unresolved outcome must identify remaining issues")
        return self


class DayPresentation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    day_number: int = Field(ge=1, le=366)
    summary: str = Field(min_length=1, max_length=2000)
    entity_ids: list[str] = Field(default_factory=list, max_length=128)
    summary_parts: list[str] = Field(default_factory=list, max_length=8,
        description="Select grounded day sections from the supplied catalog, in the desired reading order. Summary must be these sections joined with single spaces; empty uses the default summary.")


class PresentationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checked_draft_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    introduction: str = Field(min_length=1, max_length=3000)
    days: list[DayPresentation] = Field(default_factory=list, max_length=366)
    practical_notes: list[str] = Field(default_factory=list, max_length=12,
        description="Select useful notes verbatim from the checked practical-note catalog; authoritative warnings remain attached separately.")

    @model_validator(mode="after")
    def unique_days(self):
        if len({item.day_number for item in self.days}) != len(self.days):
            raise ValueError("Duplicate presentation day")
        return self

    def check_targets(self, *, draft_hash: str, day_entities: dict[int, set[str]]):
        """Check identity scope; prose factuality requires separate validation."""
        if self.checked_draft_hash != draft_hash:
            raise ValueError("Presentation belongs to another checked draft")
        for day in self.days:
            if day.day_number not in day_entities or not set(day.entity_ids) <= day_entities[day.day_number]:
                raise ValueError("Presentation references a day or entity outside the checked plan")
