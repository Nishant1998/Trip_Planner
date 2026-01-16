"""Internal compact state and non-destructive merge helpers for V2."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models.research import (
    POI,
    ResolvedStop,
    RouteEvidence,
    StayBase,
    TransportRequirement,
    WeatherContext,
)


class ResearchObservation(BaseModel):
    id: str
    tool: str
    status: str
    summary: str
    evidence_ids: list[str] = Field(default_factory=list)


class ResearchNotebook(BaseModel):
    interpretation: str = ""
    resolved_stops: list[ResolvedStop] = Field(default_factory=list)
    pois_by_stop: dict[str, list[POI]] = Field(default_factory=dict)
    routes: list[RouteEvidence] = Field(default_factory=list)
    transport_requirements: list[TransportRequirement] = Field(default_factory=list)
    weather: list[WeatherContext] = Field(default_factory=list)
    stay_bases: list[StayBase] = Field(default_factory=list)
    financial_context: dict[str, Any] = Field(default_factory=dict)
    current_facts: list[dict[str, Any]] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ResearchDelta(BaseModel):
    interpretation: str | None = None
    resolved_stops: list[ResolvedStop] = Field(default_factory=list)
    pois_by_stop: dict[str, list[POI]] = Field(default_factory=dict)
    routes: list[RouteEvidence] = Field(default_factory=list)
    transport_requirements: list[TransportRequirement] = Field(default_factory=list)
    weather: list[WeatherContext] = Field(default_factory=list)
    stay_bases: list[StayBase] = Field(default_factory=list)
    financial_context: dict[str, Any] = Field(default_factory=dict)
    current_facts: list[dict[str, Any]] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class CheckpointStop(BaseModel):
    """An LLM-selected semantic role for a factual place artifact."""

    place_ref: str
    role: Literal[
        "base", "route_member", "poi_destination", "excursion", "terminal"
    ]
    # The LLM supplies this semantic relationship; Python never guesses it.
    parent_stop_ref: str | None = None
    requirement_ids: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1, max_length=500)
    confidence: Literal["high", "medium", "low"] = "medium"


class CheckpointCandidate(BaseModel):
    """One selected candidate and only its own requirement links."""

    place_ref: str
    requirement_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class CheckpointCandidateGroup(BaseModel):
    """A selected, non-destructive candidate group for one checkpointed stop."""

    stop_ref: str
    candidates: list[CheckpointCandidate] = Field(default_factory=list)
    # Legacy checkpoint support. Group requirement IDs apply only when this
    # list has exactly one reference; they must never leak across a group.
    place_refs: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class CheckpointFact(BaseModel):
    subject_ref: str | None = None
    weather_ref: str | None = None
    fact_type: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=1200)
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"


class CheckpointTransport(BaseModel):
    leg_kind: Literal["entry", "intercity", "exit", "local_access"]
    origin_label: str = Field(min_length=1, max_length=300)
    destination_label: str = Field(min_length=1, max_length=300)
    preferred_mode: str = Field(min_length=1, max_length=80)
    status: Literal["verified", "unsupported_by_provider", "unavailable", "missing"]
    origin_place_ref: str | None = None
    destination_place_ref: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class CheckpointStayBase(BaseModel):
    """A bounded area or lodging candidate, never a live booking claim."""

    place_ref: str
    rationale: str = Field(min_length=1, max_length=500)


class ResearchCheckpoint(BaseModel):
    """Small reference-based checkpoint accepted from the research LLM."""

    interpretation: str | None = Field(default=None, max_length=2000)
    stops: list[CheckpointStop] = Field(default_factory=list)
    candidate_groups: list[CheckpointCandidateGroup] = Field(default_factory=list)
    facts: list[CheckpointFact] = Field(default_factory=list)
    transport: list[CheckpointTransport] = Field(default_factory=list)
    route_refs: list[str] = Field(default_factory=list)
    weather_refs: list[str] = Field(default_factory=list)
    stay_base_refs: list[str] = Field(default_factory=list)
    stay_bases: list[CheckpointStayBase] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class MandatoryResolution(BaseModel):
    requirement_id: str
    status: Literal["represented", "unresolved"]
    note: str | None = Field(default=None, max_length=1000)


class ResearchSubmission(BaseModel):
    """Small semantic finish payload compiled into the existing ResearchReport."""

    proposed_status: Literal["complete", "partial", "blocked"] = "partial"
    destination_interpretation: str = Field(min_length=1, max_length=3000)
    primary_stop_refs: list[str] = Field(default_factory=list)
    mandatory_requirement_resolutions: list[MandatoryResolution] = Field(default_factory=list)
    stay_base_refs: list[str] = Field(default_factory=list)
    selected_route_refs: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unresolved_gaps: list[str] = Field(default_factory=list)
    summary_notes: list[str] = Field(default_factory=list)


def _merge_by_id(existing: list[Any], incoming: list[Any], key: str = "id") -> list[Any]:
    indexed = {getattr(value, key): value for value in existing}
    order = [getattr(value, key) for value in existing]
    for value in incoming:
        identity = getattr(value, key)
        old = indexed.get(identity)
        if old is None:
            indexed[identity] = value
            order.append(identity)
            continue
        # Incoming fields enrich the prior object; lists are unioned below.
        merged = old.model_dump()
        for field_name, field_value in value.model_dump().items():
            if isinstance(field_value, list):
                merged[field_name] = list(dict.fromkeys((merged.get(field_name) or []) + field_value))
            elif field_value not in (None, "", {}, []):
                merged[field_name] = field_value
        indexed[identity] = old.__class__.model_validate(merged)
    return [indexed[identity] for identity in order]


def _append_unique(existing: list[Any], incoming: list[Any]) -> list[Any]:
    """Append scalar or structured notebook values without requiring them to be hashable."""
    merged = list(existing)
    seen = {
        json.dumps(value, sort_keys=True, default=str)
        for value in existing
    }
    for value in incoming:
        identity = json.dumps(value, sort_keys=True, default=str)
        if identity not in seen:
            seen.add(identity)
            merged.append(value)
    return merged


def merge_notebook(current: ResearchNotebook, delta: ResearchDelta) -> ResearchNotebook:
    result = current.model_copy(deep=True)
    if delta.interpretation:
        result.interpretation = delta.interpretation
    result.resolved_stops = _merge_by_id(result.resolved_stops, delta.resolved_stops)
    for stop_id, pois in delta.pois_by_stop.items():
        existing = result.pois_by_stop.get(stop_id, [])
        by_google = {poi.google_place_id: poi for poi in existing if poi.google_place_id}
        retained = list(existing)
        for poi in pois:
            old = by_google.get(poi.google_place_id) if poi.google_place_id else None
            if old is None and not any(item.id == poi.id for item in retained):
                retained.append(poi)
            elif old is not None:
                retained = [
                    old.__class__.model_validate(
                        {
                            **old.model_dump(),
                            **{
                                key: value
                                for key, value in poi.model_dump().items()
                                if value not in (None, "", [], {})
                            },
                            "evidence_ids": list(
                                dict.fromkeys(old.evidence_ids + poi.evidence_ids)
                            ),
                            "unresolved_fields": list(
                                dict.fromkeys(
                                    old.unresolved_fields + poi.unresolved_fields
                                )
                            ),
                        }
                    )
                    if item.id == old.id
                    else item
                    for item in retained
                ]
        result.pois_by_stop[stop_id] = retained
    route_key = lambda route: f"{route.origin_id}:{route.destination_id}:{route.mode}"
    existing_routes = {route_key(route): route for route in result.routes}
    route_order = [route_key(route) for route in result.routes]
    for route in delta.routes:
        identity = route_key(route)
        if identity not in existing_routes:
            route_order.append(identity)
            existing_routes[identity] = route
        else:
            existing_routes[identity] = _merge_by_id(
                [existing_routes[identity]], [route], key="origin_id"
            )[0]
    result.routes = [existing_routes[identity] for identity in route_order]
    result.transport_requirements = _merge_by_id(result.transport_requirements, delta.transport_requirements)
    result.weather = _merge_by_id(result.weather, delta.weather, key="stop_id")
    result.stay_bases = _merge_by_id(result.stay_bases, delta.stay_bases, key="stop_id")
    result.financial_context = {**result.financial_context, **delta.financial_context}
    for name in ("current_facts", "assumptions", "open_questions", "notes"):
        setattr(result, name, _append_unique(getattr(result, name), getattr(delta, name)))
    return result
