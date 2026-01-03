"""Deterministic acceptance rules for AI-extracted trip details."""

from __future__ import annotations

from app.models.api import ExtractedFieldItem, ExtractionOutput, MappingAmbiguity
from app.models.enums import FieldConfidence, FieldSource
from app.models.memory import PendingClarification, TripMemory

_EXPLICIT_TIER_LANGUAGE = {
    "budget": ("budget hotel", "cheap stay", "low-cost stay"),
    "mid_range": ("mid-range", "mid range", "moderate hotel"),
    "premium": ("premium", "upscale", "high-end"),
    "luxury": ("luxury", "five-star", "five star", "5-star", "5 star"),
}


def select_safe_extracted_fields(
    extraction: ExtractionOutput,
) -> tuple[list[ExtractedFieldItem], list[dict[str, object]]]:
    """Keep only clear, unambiguous details and explain held-back values.

    The extraction model may identify useful possibilities, but it may not turn
    an estimate or a field-mapping ambiguity into saved trip data. Other clear
    details in the same message are still retained.
    """

    ambiguous_fields = set(
        extraction.mapping_ambiguity.candidate_fields
        if extraction.mapping_ambiguity is not None
        else []
    )
    accepted: list[ExtractedFieldItem] = []
    held_back: list[dict[str, object]] = []

    for item in extraction.extracted_fields:
        # A genuine mapping ambiguity is always the first thing to explain.
        # Otherwise an ambiguous preference can be reported as a different
        # problem and the user's answer will not resolve the actual question.
        if item.field_name in ambiguous_fields:
            held_back.append(
                _held_back_payload(
                    item,
                    "This could mean more than one trip detail, so I need to clarify it first.",
                    "ambiguous_field_mapping",
                )
            )
            continue
        if item.field_name in {
            "spending_tier",
            "hotel_tier",
        } and not _has_explicit_tier_language(item):
            held_back.append(
                _held_back_payload(
                    item,
                    "This is a personal comfort preference, so it will not be guessed from the budget.",
                    "unsupported_preference_inference",
                )
            )
            continue
        # "Soft" describes the model's confidence, not necessarily the
        # traveller's wording.  Do not make someone reconfirm a detail they
        # plainly typed themselves (for example exact dates or a stated diet).
        # We still hold back model guesses, inferred values, and values with no
        # supporting user text.
        if item.confidence == FieldConfidence.SOFT and not (
            item.source == FieldSource.USER and (item.raw_text or "").strip()
        ):
            held_back.append(
                _held_back_payload(
                    item,
                    "This looks like an estimate or assumption, so please confirm it before I save it.",
                    "needs_user_confirmation",
                )
            )
            continue
        accepted.append(item)

    return accepted, held_back


def _has_explicit_tier_language(item: ExtractedFieldItem) -> bool:
    """Accept a small fixed preference enum only when the user said it plainly."""

    if item.source != FieldSource.USER:
        return False
    raw_text = (item.raw_text or "").casefold()
    value = str(item.value).casefold()
    return any(phrase in raw_text for phrase in _EXPLICIT_TIER_LANGUAGE.get(value, ()))


def remember_mapping_ambiguity(
    memory: TripMemory,
    ambiguity: MappingAmbiguity | None,
) -> None:
    """Persist one ambiguity so the next reply is treated as an answer."""

    if ambiguity is None:
        return
    candidate_fields = [field for field in ambiguity.candidate_fields if field]
    memory.internal.pending_clarification = PendingClarification(
        active=True,
        variable=candidate_fields[0] if candidate_fields else None,
        options=candidate_fields or None,
        clarification_question=_clarification_question(ambiguity),
    )


def clear_resolved_mapping_ambiguity(
    memory: TripMemory,
    accepted_fields: list[ExtractedFieldItem],
) -> None:
    """Clear pending state only after an answer updates one disputed detail."""

    pending = memory.internal.pending_clarification
    if not pending.active:
        return
    disputed_fields = {
        field for field in [pending.variable, *(pending.options or [])] if field
    }
    if any(item.field_name in disputed_fields for item in accepted_fields):
        memory.internal.pending_clarification = PendingClarification()


def _held_back_payload(
    item: ExtractedFieldItem,
    reason: str,
    reason_code: str,
) -> dict[str, object]:
    return {
        "field_name": item.field_name,
        "candidate_value": item.value,
        "reason": reason,
        "reason_code": reason_code,
        "ask_user_again": True,
    }


def _clarification_question(ambiguity: MappingAmbiguity) -> str:
    candidates = [
        field.replace("_", " ") for field in ambiguity.candidate_fields if field
    ]
    if len(candidates) >= 2:
        return (
            f"When you said '{ambiguity.raw_text}', did you mean "
            f"{candidates[0]} or {candidates[1]}?"
        )
    if candidates:
        return f"When you said '{ambiguity.raw_text}', was that about {candidates[0]}?"
    return f"Just to confirm, what did you mean by '{ambiguity.raw_text}'?"
