"""Frontend-independent normalization of the final, post-review plan."""

from __future__ import annotations


def _coords(value):
    point = getattr(value, "coordinates", None)
    return {"latitude": point.latitude, "longitude": point.longitude} if point else None


def _image_ref(image, google_place_id=None):
    """Only stable provider identity; never a short-lived image URL."""
    if not isinstance(image, dict):
        return (
            {"provider": "google_places", "place_id": google_place_id,
             "resolution_strategy": "resolve_at_display_time"}
            if google_place_id else None
        )
    value = {
        key: image[key]
        for key in ("provider", "place_id", "photo_reference", "resolution_strategy")
        if image.get(key)
    }
    return value or None


def display_trip(result, world, mandatory_allocation, final_validation):
    pois = {p.id: p for p in world.input.pois}
    stops = {s.id: s for s in world.input.stops}
    days = []
    for day in result.days:
        timeline = []
        for block in day.timeline:
            poi = pois.get(block.poi_id)
            timeline.append(
                {
                    "id": block.id,
                    "type": block.type.value.upper(),
                    "start_time": block.start_time,
                    "end_time": block.end_time,
                    "duration_minutes": block.duration_minutes,
                    "title": poi.name
                    if poi
                    else block.type.value.replace("_", " ").title(),
                    "subtitle": None,
                    "poi_id": block.poi_id,
                    "origin_id": block.origin_id,
                    "destination_id": block.destination_id,
                    "transport_mode": None,
                    "status": block.status,
                    "notes": block.notes,
                }
            )
        activities = []
        provisional = getattr(day, "provisional_activities", [])
        provisional_ids = {item.poi_id for item in provisional}
        for activity in [*day.activities, *provisional]:
            poi = pois.get(activity.poi_id)
            coordinates = _coords(poi)
            booking_notes = []
            if poi and getattr(poi, "reservation_required", None):
                booking_notes.append("Reservation or session is required; confirm availability before travel.")
            if poi and getattr(poi, "booking_lead_time", None):
                booking_notes.append(f"Suggested booking lead time: {poi.booking_lead_time}.")
            activities.append(
                {
                    "poi_id": activity.poi_id,
                    "name": activity.name,
                    "categories": (poi.normalized_categories or poi.categories)
                    if poi
                    else [],
                    "mandatory": activity.mandatory,
                    "status": "requires_confirmation" if activity.poi_id in provisional_ids else "scheduled",
                    "notes": getattr(activity, "notes", []),
                    "start_time": activity.start_time,
                    "end_time": activity.end_time,
                    "visit_duration_minutes": activity.planned_visit_minutes,
                    "visit_duration_basis": getattr(poi, "visit_duration_basis", "unknown"),
                    "visit_duration_confidence": getattr(poi, "visit_duration_confidence", "unknown"),
                    "address": activity.address,
                    "latitude": coordinates["latitude"] if coordinates else None,
                    "longitude": coordinates["longitude"] if coordinates else None,
                    "google_place_id": getattr(poi, "google_place_id", None),
                    "opening_status": activity.opening_hours_status,
                    "booking_notes": booking_notes,
                    "accessibility_notes": poi.accessibility_notes if poi else [],
                    "restrictions": poi.restrictions if poi else [],
                    "image_ref": _image_ref(
                        poi.image if poi else None,
                        getattr(poi, "google_place_id", None),
                    ),
                    "requirement_ids": activity.requirement_ids,
                }
            )
        legs = []
        for leg in day.transport_legs:
            origin = pois.get(leg.origin_id) or stops.get(leg.origin_id)
            destination = pois.get(leg.destination_id) or stops.get(leg.destination_id)
            legs.append(
                {
                    "origin_id": leg.origin_id,
                    "origin_name": leg.origin_name,
                    "origin_coordinates": _coords(origin),
                    "origin_google_place_id": getattr(origin, "google_place_id", None),
                    "destination_id": leg.destination_id,
                    "destination_name": leg.destination_name,
                    "destination_coordinates": _coords(destination),
                    "destination_google_place_id": getattr(destination, "google_place_id", None),
                    "movement_class": (
                        leg.mode_resolution.movement_class.value
                        if leg.mode_resolution
                        else "unknown"
                    ),
                    "selected_mode": leg.mode,
                    "start_time": getattr(leg, "start_time", "unknown"),
                    "end_time": getattr(leg, "end_time", "unknown"),
                    "duration_minutes": leg.duration_minutes,
                    "distance_km": leg.distance_km,
                    "status": leg.status,
                    "buffer_minutes": leg.buffer_minutes,
                    "uncertainty": leg.uncertainty,
                    "provenance": getattr(leg, "provenance", "unknown"),
                }
            )
        days.append(
            {
                "day_number": day.day_number,
                "date": day.date,
                "title": day.title,
                "theme": day.title,
                "day_kind": day.day_kind.value,
                "base_id": day.end_anchor_id,
                "base_name": day.overnight_base,
                "start_anchor": day.start_anchor,
                "end_anchor": day.end_anchor,
                "start_time": day.day_start,
                "end_time": day.day_end,
                "schedule_state": day.schedule_state,
                "timeline": timeline,
                "activities": activities,
                "transport_legs": legs,
                "meals_rest": day.meals_rest,
                "warnings": day.warnings,
                "unresolved_items": day.confirm_before_visit,
                "summary": f"{len(day.activities)} scheduled activities; {len(provisional)} proposed visits awaiting confirmation; {len(legs)} transport legs.",
            }
        )
    items = []
    for requirement in world.input.requirements:
        mapped = [
            poi.id for poi in world.input.pois if requirement.id in poi.requirement_ids
        ]
        allocations = {
            poi_id: mandatory_allocation.get(poi_id, {}) for poi_id in mapped
        }
        statuses = {value.get("status", "UNSATISFIED") for value in allocations.values()}
        if requirement.id in result.satisfied_mandatory_requirements:
            status = "SATISFIED"
        elif "ALLOCATED_UNRESOLVED" in statuses:
            status = "UNRESOLVED"
        else:
            status = "UNSATISFIED"
        items.append(
            {
                "requirement_id": requirement.id,
                "label": requirement.requirement_text,
                "type": requirement.requirement_type,
                "status": status,
                "poi_id": mapped[0] if mapped else None,
                "poi_ids": mapped,
                "day_number": next((value.get("day_number") for value in allocations.values() if value.get("day_number") is not None), None),
                "allocations": allocations,
                "reason": next((value.get("reason") for value in allocations.values() if value.get("reason")), None),
            }
        )
    unresolved = [
        {
            "type": "mandatory_excursion",
            "severity": "HIGH",
            "day_number": value.get("day_number"),
            "entity_ids": [poi_id],
            "message": value.get("reason"),
        }
        for poi_id, value in mandatory_allocation.items()
        if value.get("status") == "ALLOCATED_UNRESOLVED"
    ]
    unresolved += [
        {
            "type": "external_uncertainty",
            "severity": "MEDIUM",
            "day_number": None,
            "entity_ids": [],
            "message": message,
        }
        for message in result.uncertainties
    ]
    return {
        "status": final_validation.get("status", "BEST_EFFORT"),
        "delivery_status": final_validation.get("delivery_status", "DRAFT_NEEDS_CONFIRMATION"),
        "trip_summary": {
            "title": result.trip_summary,
            "start_date": world.input.start_date,
            "end_date": world.input.end_date,
            "duration_days": world.input.duration_days,
            "origin": world.input.origin,
            "end_location": getattr(world.input, "end_location", None)
            or world.input.origin,
            "timezone": getattr(world.input, "timezone", None),
            "travellers": world.input.travellers,
            "currency": world.input.currency,
        },
        "route_summary": {
            "bases": result.selected_bases,
            "structural_complete": final_validation.get("structural_complete"),
            "missing_legs": final_validation.get("missing_legs", 0),
        },
        "bases": [
            {
                "id": stop.id,
                "name": stop.name,
                "coordinates": _coords(stop),
                "google_place_id": getattr(stop, "google_place_id", None),
            }
            for stop in world.input.stops
            if stop.id in world.base_stop_ids
        ],
        "days": days,
        "mandatory_summary": {
            "total": len(items),
            "satisfied": sum(item["status"] == "SATISFIED" for item in items),
            "unresolved": sum(
                item["status"] == "UNRESOLVED" for item in items
            ),
            "unsatisfied": sum(item["status"] == "UNSATISFIED" for item in items),
            "items": items,
        },
        "budget_summary": {
            "trip_budget": world.input.budget,
            "known_cost_total": result.budget_summary.known_attraction_cost,
            "known_cost_currency": result.budget_summary.currency,
            "budget_status": result.budget_summary.status,
            "categories": getattr(result.budget_summary, "categories", {}),
            "total_verified": getattr(
                result.budget_summary,
                "total_verified",
                result.budget_summary.known_attraction_cost,
            ),
            "total_estimated": getattr(result.budget_summary, "total_estimated", 0),
            "unknown_cost_components": getattr(
                result.budget_summary,
                "unknown_categories",
                result.budget_summary.notes,
            ),
        },
        "guidance": {
            "booking_and_recheck": list(
                dict.fromkeys([
                    *result.uncertainties,
                    *result.warnings,
                    *getattr(world.input, "unresolved_gaps", []),
                ])
            ),
            "current_facts": getattr(world.input, "current_facts", []),
        },
        "warnings": result.warnings,
        "unresolved_items": unresolved,
    }
