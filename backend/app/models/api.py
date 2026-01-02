"""Shared Pydantic contracts used across the app runtime."""

from __future__ import annotations

from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, Field, model_validator

# Imports the shared response sanitizer so the ConversationAgentOutput validator can strip jargon at construction time.
from app.collection.utils.sanitizer import sanitize_user_facing_response
from app.models.enums import FieldConfidence, FieldSource


# ===== JSON Value Shapes =====

JsonObject: TypeAlias = dict[str, Any]


# ===== HTTP Boundary Models =====


class StoredMessage(BaseModel):
    """Serialized chat message returned to the frontend."""

    role: str
    content: str
    created_at: str
    meta: JsonObject = Field(default_factory=dict)


class TripStateResponse(BaseModel):
    """Full frontend state snapshot for one trip."""

    trip_id: int
    slug: str
    status: str
    messages: list[StoredMessage] = Field(default_factory=list)
    itinerary: JsonObject | None = None
    checkpoint: JsonObject | None = None


class PlannerHandoffResponse(BaseModel):
    """Temporary debug response containing the current planner handoff payload."""

    trip_id: int
    slug: str
    status: str
    handoff: JsonObject | None = None
    updated_at: str | None = None


class AcceptedMessageResponse(BaseModel):
    """Acknowledgement returned after queueing one user message."""

    accepted: bool = True
    trip_id: int
    slug: str


class ChatSendRequest(BaseModel):
    """Incoming user message payload."""

    text: str
    client_message_id: str | None = Field(default=None, max_length=64)


class StatusEventPayload(BaseModel):
    """Live status event payload for the frontend."""

    status: str
    message: str | None = None


class StateChangePayload(BaseModel):
    """Trip-state transition event payload."""

    old_status: str
    new_status: str


class ErrorPayload(BaseModel):
    """Error event payload for the live stream."""

    message: str


class TraceEventPayload(BaseModel):
    """One safe, compact enter/exit/error observation for a runtime block."""

    sequence: int | None = None
    timestamp: str
    block: str
    stage: Literal["enter", "exit", "error"]
    input: Any = None
    output: Any = None
    message: str = ""


# ===== Agent Input And Output Models =====


class PrefillField(BaseModel):
    value: Any
    source_hint: str | None = None  # short paraphrase of profile evidence


PrefillOmissionReason: TypeAlias = Literal[
    "single_trip",
    "conditional",
    "conflicting",
    "insufficient",
]


class PrefillOutput(BaseModel):
    prefilled_fields: dict[str, PrefillField] = Field(default_factory=dict)
    unprefilled_with_reason: dict[str, PrefillOmissionReason] = Field(
        default_factory=dict
    )


class ExtractedFieldItem(BaseModel):
    """One extracted field patch produced from a user turn."""

    field_name: str
    value: Any
    source: FieldSource = FieldSource.USER
    is_correction: bool = False
    confidence: FieldConfidence = FieldConfidence.HARD
    currency_mentioned: str | None = None
    raw_text: str | None = None


class AmbiguityDetected(BaseModel):
    """Ambiguity details that require one user clarification."""

    active: bool
    variable: str | None = None
    options: list[str] | None = None
    clarification_question: str | None = None


class MappingAmbiguity(BaseModel):
    """Field-mapping ambiguity that needs one clarification before writing safely."""

    raw_text: str
    candidate_fields: list[str] = Field(default_factory=list)


class ExtractionOutput(BaseModel):
    """Normalized extraction result for one user message."""

    extracted_fields: list[ExtractedFieldItem] = Field(default_factory=list)
    mapping_ambiguity: MappingAmbiguity | None = None
    profile_signals: list[str] = Field(default_factory=list)
    trip_context_notes: list[str] = Field(default_factory=list)
    is_correction: bool = False

    @model_validator(mode="after")
    def _finalize_flags(self) -> "ExtractionOutput":
        """Backfill lightweight runtime flags derived from the extracted fields."""

        if not self.is_correction:
            self.is_correction = any(
                item.is_correction for item in self.extracted_fields
            )

        return self

    def to_legacy_ambiguity(self) -> AmbiguityDetected | None:
        """Convert mapping ambiguity into the older clarification shape when needed."""

        if self.mapping_ambiguity is None:
            return None

        candidates = [item for item in self.mapping_ambiguity.candidate_fields if item]
        pretty_candidates = [item.replace("_", " ") for item in candidates]

        if len(pretty_candidates) >= 2:
            clarification_question = (
                f"When you said '{self.mapping_ambiguity.raw_text}', were you talking about "
                f"{pretty_candidates[0]} or {pretty_candidates[1]}?"
            )
        elif len(pretty_candidates) == 1:
            clarification_question = (
                f"When you said '{self.mapping_ambiguity.raw_text}', was that about "
                f"{pretty_candidates[0]}?"
            )
        else:
            clarification_question = f"Just to confirm, what did you mean by '{self.mapping_ambiguity.raw_text}'?"

        return AmbiguityDetected(
            active=True,
            variable=candidates[0] if candidates else None,
            options=pretty_candidates or None,
            clarification_question=clarification_question,
        )

    def field_names(self) -> list[str]:
        """Return the ordered field names extracted from the latest turn."""

        return [item.field_name for item in self.extracted_fields]


class PreClassifierOutput(BaseModel):
    """Route decision returned before the extraction hot path runs."""

    route: Literal["run_extraction", "run_response"]
    low_confidence: bool = False
    advice_requested: bool = False


class ResponseOutput(BaseModel):
    """One assistant response generated during collection."""

    response_text: str


class ConversationAgentOutput(BaseModel):
    """Output from the conversation agent (Riya) for one turn."""

    response_text: str
    ready_for_confirmation: bool = False
    field_asked: str | None = None

    @model_validator(mode="after")
    def _normalize_and_sanitize(self) -> "ConversationAgentOutput":
        """Coerce LLM string literals and strip jargon/enum codes from the response.

        1. Coerce 'null'/'none'/'' strings in field_asked to Python None.
        2. Run the shared response sanitizer on response_text so that
           jargon and raw enum codes are stripped for *all* callers —
           including direct agent calls in live tests that bypass the
           orchestrator pipeline.
        """
        # Normalize field_asked.
        if isinstance(self.field_asked, str) and self.field_asked.lower().strip() in {
            "null",
            "none",
            "",
        }:
            self.field_asked = None

        # Sanitize response text.
        if self.response_text:
            self.response_text = sanitize_user_facing_response(self.response_text)

        return self


class ConfirmationIntentOutput(BaseModel):
    """Intent classification for a confirmation reply."""

    intent: Literal["confirm", "change", "add", "advice", "unclear"]
    change_fields: list[str] | None = None
    raw_change_text: str | None = None


# Replaces the old RouteModeCheckOutput model with the richer DestinationIntelligenceOutput that merges context, route feasibility, and timing into one agent response.
class DestinationIntelligenceOutput(BaseModel):
    """Combined destination context, route feasibility, and seasonal timing output."""

    # Section 1 — soft destination planning context.
    ideal_duration_range: str | None = None
    best_months: list[str] = Field(default_factory=list)
    suggested_long_distance_modes: list[str] = Field(default_factory=list)
    suggested_local_transport: list[str] = Field(default_factory=list)
    budget_signal: str | None = None
    planning_notes: list[str] = Field(default_factory=list)
    # Section 1b — worldwide geography context, sourced by the same research.
    trip_scope: Literal["domestic", "international"] | None = None
    suggested_budget_currency: str | None = None
    primary_destination_timezone: str | None = None
    destination_timezones: dict[str, str] = Field(default_factory=dict)
    # Section 2 — route feasibility.
    route_too_long_for_self_drive: bool = False
    route_note: str | None = None
    # True only when the traveller's selected transport modes cannot complete
    # the main route in practice.  This is distinct from a mere preference.
    transport_should_block: bool = False
    transport_warning: str | None = None
    # Section 3 — seasonal timing.
    season_should_warn: bool = False
    season_warning: str | None = None
    safety_advisories: list[str] = Field(default_factory=list)
    # A strict, verified visit blocker. Kept separate from soft weather advice.
    access_should_block: bool = False
    access_warning: str | None = None


class ProfileUpdateOutput(BaseModel):
    """Structured profile delta returned after trip completion."""

    updated: list[str] = Field(default_factory=list)
    new: list[str] = Field(default_factory=list)


class TripMetadataOutput(BaseModel):
    """Generated trip name, description, and scope after trip confirmation."""

    trip_name: str | None = None
    trip_description: str
    trip_scope: str | None = None


class TripFeasibilityEstimate(BaseModel):
    """Budget and duration estimate for one trip concept."""

    min_days: int
    max_days: int
    min_budget_inr: int
    max_budget_inr: int
    reasoning: str
    destination_notes: list[str] = Field(default_factory=list)
