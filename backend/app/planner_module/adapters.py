"""Input conversion: TripMemory to ResearchInput, then report to PlannerInput."""

from __future__ import annotations

from datetime import date
import re
from typing import Any

from app.planner_module.models.research import (
    AccommodationPreferences,
    Budget,
    Destination,
    ResearchInput,
    TimePreferences,
    TransportPreferences,
    Travellers,
)

from .models.planning import (
    MandatoryRequirement,
    PlannerInput,
    PlanningPOI,
    PlanningStop,
    PlanningWorld,
    RouteFact,
)


def _unwrap(value: Any) -> Any:
    return value.get("value") if isinstance(value, dict) and "value" in value else value


def _list(value: Any) -> list[Any]:
    value = _unwrap(value)
    return (
        value if isinstance(value, list) else ([] if value in (None, "") else [value])
    )


def _source(section: dict[str, Any], name: str, default: str = "default") -> str:
    value = section.get(name)
    source = value.get("source") if isinstance(value, dict) else None
    return str(source) if source else default


def _date(value: Any) -> date:
    return date.fromisoformat(str(_unwrap(value)))


def _int_or_default(value: Any, default: int = 0) -> int:
    """Convert a collected numeric value while treating an explicit null as unset."""
    value = _unwrap(value)
    return default if value in (None, "") else int(value)


def trip_memory_to_research_input(memory: dict[str, Any]) -> ResearchInput:
    """Preserve every planning-relevant field from canonical or already-clean memory."""
    if "needed" not in memory:
        return ResearchInput.model_validate(memory)
    metadata = memory.get("metadata", {}) or {}
    needed = memory.get("needed", {}) or {}
    optional = memory.get("optional", {}) or {}
    internal = memory.get("internal", {}) or {}
    raw_destination = _unwrap(needed.get("destination")) or {}
    if not isinstance(raw_destination, dict):
        raw_destination = {"type": "open", "items": _list(raw_destination)}
    destination_type = str(raw_destination.get("type", "open")).lower()
    if destination_type not in {
        "city",
        "cities",
        "country",
        "region",
        "route",
        "poi",
        "open",
    }:
        raise ValueError(f"Unsupported destination.type: {destination_type}")
    start, end = _date(needed["start_date"]), _date(needed["end_date"])
    sources = {
        name: _source(section, name)
        for section in (needed, optional, metadata)
        for name in section
    }
    return ResearchInput(
        origin=str(_unwrap(needed.get("origin", ""))),
        destination=Destination(
            type=destination_type,
            items=[str(item) for item in _list(raw_destination.get("items"))],
            subtype=raw_destination.get("subtype"),
        ),
        start_date=start,
        end_date=end,
        duration_days=int(
            _unwrap(needed.get("trip_duration_days", (end - start).days + 1))
        ),
        end_location=_unwrap(optional.get("end_location")),
        trip_scope=_unwrap(metadata.get("trip_scope")),
        timezone=_unwrap(metadata.get("timezone")),
        currency_code=str(_unwrap(metadata.get("currency_code", "INR"))),
        travellers=Travellers(
            num_adults=_int_or_default(needed.get("num_adults"), default=1),
            num_children=_int_or_default(
                optional.get("num_children", needed.get("num_children", 0))
            ),
            children_ages=[int(age) for age in _list(optional.get("children_ages"))],
            num_seniors=_int_or_default(
                optional.get("num_seniors", needed.get("num_seniors", 0))
            ),
            accessibility_needs=[
                str(value) for value in _list(optional.get("accessibility_needs"))
            ],
        ),
        trip_purpose=_unwrap(optional.get("trip_purpose", needed.get("trip_purpose"))),
        activity_interests=[
            str(value) for value in _list(optional.get("activity_interests"))
        ],
        must_see=[str(value) for value in _list(optional.get("must_see"))],
        avoid=[str(value) for value in _list(optional.get("avoid"))],
        cuisine_preferences=[
            str(value) for value in _list(optional.get("cuisine_prefs"))
        ],
        dietary_restrictions=[
            str(value) for value in _list(optional.get("dietary_restrictions"))
        ],
        travel_style=str(_unwrap(optional.get("travel_style", "moderate"))),
        time_preferences=TimePreferences(
            depart_time_window=_unwrap(optional.get("depart_time_window")),
            arrival_time_window=_unwrap(optional.get("arrival_time_window")),
            return_time_window=_unwrap(optional.get("return_time_window")),
            preferred_day_start=_unwrap(optional.get("day_start_time")),
            preferred_day_end=_unwrap(optional.get("day_end_time")),
        ),
        budget=Budget(
            total=_unwrap(needed.get("trip_budget")),
            currency=str(_unwrap(metadata.get("currency_code", "INR"))),
            spending_tier=_unwrap(optional.get("spending_tier")),
        ),
        transport_preferences=TransportPreferences(
            local_modes=[
                str(value)
                for value in _list(
                    optional.get("local_transport_modes", optional.get("local_modes"))
                )
            ],
            long_distance_modes=[
                str(value) for value in _list(optional.get("long_distance_modes"))
            ],
            train_class=_unwrap(optional.get("train_class")),
            flight_cabin_class=_unwrap(optional.get("flight_cabin_class")),
        ),
        accommodation_preferences=AccommodationPreferences(
            hotel_tier=_unwrap(optional.get("hotel_tier")),
            lodging_type=_unwrap(optional.get("lodging_type")),
            room_type=_unwrap(
                optional.get("room_type_pref", optional.get("room_type"))
            ),
        ),
        trip_notes=[str(value) for value in _list(optional.get("trip_notes"))],
        trip_context_notes=[
            str(value) for value in internal.get("trip_context_notes", [])
        ],
        traveler_context={
            str(name): str(value)
            for name, value in (internal.get("traveler_context") or {}).items()
            if value not in (None, "")
        },
        destination_context={
            "sanity_state": internal.get("sanity_state", {}),
            "destination_intelligence": (internal.get("sanity_state", {}) or {}).get(
                "destination_intelligence", {}
            ),
        },
        field_sources=sources,
    )


BASE_TYPES = {
    "base",
    "city",
    "locality",
    "region",
    "country",
    "administrative_area_level_1",
    "administrative_area_level_2",
}


def _transport_mode(value: object) -> str | None:
    """Normalize a researcher-selected transport label for the route resolver."""
    text = str(value or "").casefold()
    if any(word in text for word in ("train", "rail", "shinkansen", "metro", "subway")):
        return "train"
    if "flight" in text or "plane" in text:
        return "flight"
    if "bus" in text or "transit" in text:
        return "transit"
    if any(word in text for word in ("drive", "road", "car", "taxi")):
        return "driving"
    if "walk" in text:
        return "walking"
    return None


def _duration_minutes_from_evidence(evidence: dict[str, Any]) -> int | None:
    """Read one explicit duration from immutable research evidence conservatively."""
    value = evidence.get("retrieved_value", {})
    if not isinstance(value, dict):
        return None
    text = " ".join(
        str(value.get(field, "")) for field in ("title", "snippet", "text")
    )
    # Match the most specific forms first.  For ranges, use the upper bound so
    # the scheduler never treats a best-case travel time as a guarantee.
    candidates: list[tuple[int, int]] = []
    for hour_minute in re.finditer(
        r"\b(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)\s*(?:and\s*)?(\d+)?\s*(?:minutes?|mins?)?\b",
        text,
        flags=re.IGNORECASE,
    ):
        hours = float(hour_minute.group(1))
        minutes = int(hour_minute.group(2) or 0)
        candidates.append((hour_minute.start(), max(1, round(hours * 60 + minutes))))
    for hour_range in re.finditer(
        r"\b(\d+(?:\.\d+)?)\s*(?:to|[-–])\s*(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)\b",
        text,
        flags=re.IGNORECASE,
    ):
        candidates.append((hour_range.start(), max(1, round(float(hour_range.group(2)) * 60))))
    for minute_range in re.finditer(
        r"\b(\d+)\s*(?:to|[-–])\s*(\d+)\s*(?:minutes?|mins?)\b",
        text,
        flags=re.IGNORECASE,
    ):
        candidates.append((minute_range.start(), max(1, int(minute_range.group(2)))))
    for minutes in re.finditer(r"\b(\d+)\s*(?:minutes?|mins?)\b", text, flags=re.IGNORECASE):
        candidates.append((minutes.start(), max(1, int(minutes.group(1)))))
    return min(candidates, default=(0, None), key=lambda item: item[0])[1]


def _route_facts_from_transport_evidence(report: dict[str, Any]) -> list[RouteFact]:
    """Compile explicit endpoint-linked research evidence into estimated route facts."""
    evidence_by_id = {
        item.get("id"): item
        for item in report.get("evidence", [])
        if isinstance(item, dict) and item.get("id")
    }
    facts: list[RouteFact] = []
    for requirement in report.get("transport_requirements", []):
        if not isinstance(requirement, dict):
            continue
        origin_id = requirement.get("origin_id")
        destination_id = requirement.get("destination_id")
        mode = _transport_mode(requirement.get("preferred_mode"))
        if not origin_id or not destination_id or not mode:
            continue
        evidence_ids = [
            item for item in requirement.get("route_evidence_ids", [])
            if item in evidence_by_id
        ]
        duration = next(
            (
                _duration_minutes_from_evidence(evidence_by_id[item])
                for item in evidence_ids
                if _duration_minutes_from_evidence(evidence_by_id[item]) is not None
            ),
            None,
        )
        if duration is None:
            continue
        facts.append(
            RouteFact(
                origin_id=origin_id,
                destination_id=destination_id,
                mode=mode,
                duration_minutes=duration,
                straight_line_km=0,
                status="estimated",
                provenance="research_estimated",
                evidence_ids=evidence_ids,
            )
        )
    return facts


def adapt_research_report(
    report: dict, source_reference: str = "in_memory"
) -> PlannerInput:
    """Apply the existing planner field mapping to a JSON-shaped report in memory."""
    context = report["normalized_trip_context"]
    destination = context["destination"]
    stops = [PlanningStop.model_validate(x) for x in report.get("resolved_stops", [])]
    pois = [
        PlanningPOI.model_validate(poi)
        for bucket in report.get("pois_by_stop", {}).values()
        for poi in bucket
    ]
    return PlannerInput(
        source_report_path=source_reference,
        trip_name=" / ".join(destination.get("items", ["Trip"])),
        status=report.get("status", "partial"),
        origin=context["origin"],
        end_location=context.get("end_location"),
        timezone=context.get("timezone"),
        trip_purpose=context.get("trip_purpose"),
        travel_style=context.get("travel_style", "moderate"),
        start_date=context["start_date"],
        end_date=context["end_date"],
        duration_days=context["duration_days"],
        currency=context.get("currency_code", "INR"),
        travellers=context.get("travellers", {}),
        destination_type=destination.get("type"),
        trip_scope=context.get("trip_scope"),
        activity_interests=context.get("activity_interests", []),
        avoid=context.get("avoid", []),
        cuisine_preferences=context.get("cuisine_preferences", []),
        dietary_restrictions=context.get("dietary_restrictions", []),
        accommodation_preferences=context.get("accommodation_preferences", {}),
        mandatory_constraints=report.get("mandatory_constraints", []),
        financial_context=report.get("financial_context", {}),
        transport_preferences=context.get("transport_preferences", {}),
        time_preferences=context.get("time_preferences", {}),
        budget=context.get("budget", {}),
        trip_notes=context.get("trip_notes", []),
        trip_context_notes=context.get("trip_context_notes", []),
        traveler_context=context.get("traveler_context", {}),
        stops=stops,
        pois=pois,
        requirements=[
            MandatoryRequirement.model_validate(x)
            for x in report.get("mandatory_requirements", [])
        ],
        routes=[
            RouteFact.model_validate(x)
            for x in report.get("inter_stop_routes", [])
            + report.get("intra_stop_route_evidence", [])
        ] + _route_facts_from_transport_evidence(report),
        transport_requirements=report.get("transport_requirements", []),
        evidence=report.get("evidence", []),
        clusters=report.get("geographic_clusters", []),
        stay_bases=report.get("stay_bases", []),
        weather_context=report.get("weather_context", []),
        current_facts=report.get("current_facts", []),
        assumptions=report.get("assumptions", []),
        unresolved_gaps=report.get("unresolved_gaps", []),
    )


def build_world(data: PlannerInput):
    """Build the planner's stable-ID indexes without selecting an itinerary."""
    by_stop: dict[str, list[str]] = {}
    for poi in data.pois:
        by_stop.setdefault(poi.stop_id, []).append(poi.id)
    # Research uses its explicit stop types. A mandatory POI never becomes a base.
    poi_requirement_ids = {
        req.id
        for req in data.requirements
        if req.required and req.requirement_type != "destination_or_base"
    }
    # Some Research reports express a mandatory request through the typed
    # requirement mapping rather than repeating is_mandatory on the POI.
    mandatory = [
        poi.id
        for poi in data.pois
        if poi.is_mandatory or bool(set(poi.requirement_ids) & poi_requirement_ids)
    ]
    bases = [
        stop.id
        for stop in data.stops
        if (
            stop.role == "base"
            or (stop.role is None and stop.type.casefold() in BASE_TYPES)
        )
    ]
    # Typed requirements and the POI flag must mean the same thing to the
    # scheduler; otherwise an explicitly required visit can be dropped as optional.
    mandatory_set = set(mandatory)
    data = data.model_copy(update={"pois": [
        poi.model_copy(update={"is_mandatory": True}) if poi.id in mandatory_set else poi
        for poi in data.pois
    ]})
    optional = [poi.id for poi in data.pois if poi.id not in mandatory_set]
    route_members = [
        stop.id
        for stop in data.stops
        if stop.role == "route_member"
        or (stop.role is None and stop.type.casefold() in {"route", "circuit_stop", "mandatory_poi"})
    ]
    return PlanningWorld(
        input=data,
        base_stop_ids=bases,
        mandatory_poi_ids=mandatory,
        optional_poi_ids=optional,
        pois_by_stop=by_stop,
        route_members=route_members,
    )
