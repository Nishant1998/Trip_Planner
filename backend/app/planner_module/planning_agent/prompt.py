"""Planner V2 system prompt and compact continuation context."""

from __future__ import annotations

import json
from typing import Any

from .models import PlanningNotebook

PROMPT_VERSION = "planning-agent-v2.1-journey-first"

SYSTEM_PROMPT = """You are the single Planning Agent for a confirmed trip.

ResearchReport and confirmed trip input are authoritative. You choose semantic
itinerary decisions only: bases, nights, day purpose, supplied POIs, trade-offs,
alternatives, and targeted repairs. Use only supplied IDs. Never invent routes,
durations, distances, coordinates, opening hours, prices, reservations, or exact
timestamps. Deterministic tools calculate routes, geographic order, timelines,
budget facts, and validation.

Arrival/departure continuity and mandatory requirements come before optional
sightseeing. Preserve explicit primary-base order and never add an unresearched
base or stay area. A stay-area choice must be a supplied candidate ID/name or
the explicit value "unknown". Unknown facts must remain unknown. Prefer useful
but realistic days.

Return exactly one submit_plan_proposal function call containing the COMPLETE
replacement checkpoint: base_sequence, nights_by_base, stay_area_by_night and
every day in order. The baseline is a starting suggestion, not a fixed allocation.
Plan the origin-to-end journey first. Allocate unequal city time where mandatory
visit durations and preferences justify it. Use short transfer days for compatible
visits only when travel evidence permits; do not assume every transfer takes a day.
Place every mandatory POI before adding nearby optional visits. You may move
mandatory visits between compatible days to satisfy known opening days. Keep
the destination sequence and circuit-member order. Never fill dates with fake
rest days or duplicate visits. An intentional rest day needs a reason in notes.
Night counts must match the day bases for all nights before the last day.
Use supplied stay-area names or unknown, never a made-up hotel. Leave anchor IDs
unset: the compiler derives continuous anchors from researched bases and stays.
preferred_order, when supplied, must contain every selected POI exactly once.
Prefer a realistic balance of culture, food, interests and rest, not a fixed count.
Unknown travel, hours, accessibility or budget facts must be noted, not invented.
For a selected class, workshop, tour or other session-based visit whose actual
session time is not supplied, include its ID in confirmation_required_poi_ids.
General business opening hours do not prove that a bookable session exists then.
On repair, return the whole amended checkpoint and retain every mandatory POI.
Do not
write an itinerary in prose and do not call route, schedule, budget or validation
tools: the bounded graph runs those deterministic stages after your proposal.
"""


def build_agent_context(*, world: Any, notebook: PlanningNotebook, feedback: dict | None, remaining_steps: int, include_input: bool) -> str:
    input_data = world.input
    compact = {
        "confirmed_trip": {
            "origin": input_data.origin,
            "end_location": input_data.end_location or input_data.origin,
            "dates": [input_data.start_date, input_data.end_date],
            "days": input_data.duration_days,
            "travel_style": input_data.travel_style,
            "interests": input_data.activity_interests,
            "avoid": input_data.avoid,
            "travellers": input_data.travellers,
            "transport": input_data.transport_preferences,
            "budget": input_data.budget,
            "notes": input_data.trip_context_notes,
            "trip_notes": input_data.trip_notes,
            "traveler_context": input_data.traveler_context,
            "dietary_restrictions": input_data.dietary_restrictions,
            "cuisine_preferences": input_data.cuisine_preferences,
            "time_preferences": input_data.time_preferences,
            "hard_constraints": input_data.mandatory_constraints,
        } if include_input else None,
        "bases": [
            {"id": stop.id, "name": stop.name, "requirements": stop.requirement_ids,
             "coordinates": stop.coordinates.model_dump(mode="json"), "role": stop.role}
            for stop in input_data.stops if stop.id in world.base_stop_ids
        ],
        "pois": [
            {"id": poi.id, "name": poi.name, "stop_id": poi.stop_id,
             "mandatory": poi.is_mandatory, "requirements": poi.requirement_ids,
             "categories": poi.normalized_categories or poi.categories,
             "minimum_visit_minutes": poi.minimum_visit_minutes,
             "recommended_visit_minutes": poi.recommended_visit_minutes,
             "visit_minutes": poi.visit_minutes,
             "coordinates": poi.coordinates.model_dump(mode="json"),
             "closed_weekdays": poi.closed_weekdays,
             "opening_hours": poi.opening_hours,
             "opening_periods": poi.opening_periods,
             "accessibility_notes": poi.accessibility_notes,
             "restrictions": poi.restrictions,
             "evidence_ids": poi.evidence_ids}
            for poi in input_data.pois
        ],
        "requirements": [item.model_dump(mode="json") for item in input_data.requirements],
        "routes": [item.model_dump(mode="json") for item in input_data.routes],
        "transport_requirements": input_data.transport_requirements,
        "stay_area_candidates": input_data.stay_bases,
        "current_facts": input_data.current_facts,
        "notebook": notebook.model_dump(mode="json"),
        "validator_feedback": feedback,
        "remaining_agent_steps": remaining_steps,
    }
    return json.dumps(compact, ensure_ascii=False)
