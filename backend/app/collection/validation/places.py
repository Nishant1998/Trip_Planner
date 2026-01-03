"""Conservative worldwide place validation backed by live geocoding."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, TypeAdapter

from app.models.api import ExtractedFieldItem

PLACE_CHECK_FIELDS = {"origin", "destination", "end_location"}


class PlaceGeocoder(Protocol):
    """Small provider contract required by collection validation."""

    def geocode(
        self,
        name: str,
        country_code: str | None = None,
        *,
        allow_regions: bool = False,
    ) -> Awaitable[Any]: ...


class ValidationIssue(BaseModel):
    """One field-level place validation outcome."""

    field_name: str
    input_index: int | None = None
    action: Literal["pass", "reject"]
    candidate_value: Any
    normalized_value: None = None
    reason_code: str
    reason: str | None = None
    confidence: Literal["low", "medium", "high"]


class PlaceCheckFieldInput(BaseModel):
    """One extracted field passed into place validation."""

    field_name: Literal["origin", "destination", "end_location"]
    candidate_value: Any
    raw_text: str | None = None


class PlaceCheckInput(BaseModel):
    """Batch input for one place-check operation."""

    fields: list[PlaceCheckFieldInput] = Field(default_factory=list)


class PlaceCheckEnvelope(BaseModel):
    """All place validation outcomes for one extraction turn."""

    issues: list[ValidationIssue] = Field(default_factory=list)


def build_place_check_failure_payload(
    envelope: PlaceCheckEnvelope,
) -> list[dict[str, Any]]:
    """Convert rejected outcomes into the conversation-agent handoff."""

    return [
        {
            "field_name": issue.field_name,
            "candidate_value": TypeAdapter(Any).dump_python(
                issue.candidate_value,
                mode="json",
            ),
            "reason": issue.reason,
            "reason_code": issue.reason_code,
        }
        for issue in envelope.issues
        if issue.action == "reject"
    ]


async def run_place_check(
    extracted_fields: list[ExtractedFieldItem],
    agent_check_fields: set[str],
    geocoder: PlaceGeocoder | None = None,
) -> PlaceCheckEnvelope:
    """Verify unique place names once and preserve values when tools fail.

    A definitive provider "no match" asks the user to clarify. Network,
    authentication, quota, or provider failures do not erase valid user input;
    those values pass with low confidence and remain explicitly unverified.
    """

    task_input = _build_place_check_input(extracted_fields, agent_check_fields)
    if not task_input.fields:
        return PlaceCheckEnvelope()

    queries = sorted(
        {query for field in task_input.fields for query in _field_queries(field)}
    )
    results: dict[tuple[str, bool], bool | None] = {}
    if geocoder is not None and queries:
        outcomes = await asyncio.gather(
            *(
                _resolve_query(geocoder, name, allow_regions)
                for name, allow_regions in queries
            )
        )
        results = dict(zip(queries, outcomes, strict=True))

    return PlaceCheckEnvelope(
        issues=[
            _field_issue(
                field,
                input_index=index,
                geocoder_available=geocoder is not None,
                results=results,
            )
            for index, field in enumerate(task_input.fields)
        ]
    )


def _build_place_check_input(
    extracted_fields: list[ExtractedFieldItem],
    agent_check_fields: set[str],
) -> PlaceCheckInput:
    return PlaceCheckInput(
        fields=[
            PlaceCheckFieldInput(
                field_name=field.field_name,  # type: ignore[arg-type]
                candidate_value=field.value,
                raw_text=field.raw_text,
            )
            for field in extracted_fields
            if field.field_name in PLACE_CHECK_FIELDS
            and field.field_name in agent_check_fields
        ]
    )


def _field_queries(field: PlaceCheckFieldInput) -> list[tuple[str, bool]]:
    value = field.candidate_value
    if field.field_name in {"origin", "end_location"}:
        return [(str(value).strip(), False)]

    if not isinstance(value, dict) or value.get("type") == "open":
        return []
    allow_regions = value.get("type") == "region"
    return [
        (str(item).strip(), allow_regions)
        for item in value.get("items", [])
        if str(item).strip()
    ]


async def _resolve_query(
    geocoder: PlaceGeocoder,
    name: str,
    allow_regions: bool,
) -> bool | None:
    try:
        await geocoder.geocode(name, allow_regions=allow_regions)
    except LookupError:
        return False
    except Exception:
        return None
    return True


def _field_issue(
    field: PlaceCheckFieldInput,
    *,
    input_index: int,
    geocoder_available: bool,
    results: dict[tuple[str, bool], bool | None],
) -> ValidationIssue:
    queries = _field_queries(field)
    if not queries:
        return ValidationIssue(
            field_name=field.field_name,
            input_index=input_index,
            action="pass",
            candidate_value=field.candidate_value,
            reason_code="no_place_selected",
            reason="No chosen place needs verification yet.",
            confidence="high",
        )

    if not geocoder_available:
        return _unavailable_issue(field, input_index)

    unresolved = [
        name for name, region in queries if results.get((name, region)) is False
    ]
    if unresolved:
        display = ", ".join(unresolved)
        return ValidationIssue(
            field_name=field.field_name,
            input_index=input_index,
            action="reject",
            candidate_value=field.candidate_value,
            reason_code="place_not_resolved",
            reason=(
                f"I couldn't verify {display} as a specific place. "
                "Please check the spelling or add its country or region."
            ),
            confidence="medium",
        )

    if any(results.get(query) is None for query in queries):
        return _unavailable_issue(field, input_index)

    return ValidationIssue(
        field_name=field.field_name,
        input_index=input_index,
        action="pass",
        candidate_value=field.candidate_value,
        reason_code="verified_place",
        reason="Every supplied place was resolved by the geocoding provider.",
        confidence="high",
    )


def _unavailable_issue(
    field: PlaceCheckFieldInput,
    input_index: int,
) -> ValidationIssue:
    return ValidationIssue(
        field_name=field.field_name,
        input_index=input_index,
        action="pass",
        candidate_value=field.candidate_value,
        reason_code="verification_unavailable",
        reason="Live place verification was unavailable; the user's value was preserved.",
        confidence="low",
    )
