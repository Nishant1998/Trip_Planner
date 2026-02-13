"""Order selected POIs geographically before provider routes are requested."""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

from app.models.draft import DraftActivity, ItineraryDraft
from app.models.evidence import GeoPoint, PlaceEvidence
from app.models.context import ResolvedDestination, TripSkeleton


def order_draft_pois(
    draft: ItineraryDraft,
    destination: ResolvedDestination,
    skeleton: TripSkeleton,
    places: dict[str, PlaceEvidence],
) -> ItineraryDraft:
    """Use stop order plus nearest-neighbour POI order; preserve non-POI slots."""
    updated = draft.model_copy(deep=True)
    skeleton_days = {day.day_number: day for day in skeleton.days}
    anchors = {
        stop.name.casefold(): GeoPoint(latitude=stop.latitude, longitude=stop.longitude)
        for stop in destination.stops
        if stop.latitude is not None and stop.longitude is not None
    }

    for day in updated.days:
        place_slots = [
            index
            for index, activity in enumerate(day.activities)
            if activity.evidence_place_id in places
        ]
        selected = [day.activities[index] for index in place_slots]
        skeleton_day = skeleton_days.get(day.day_number)
        stop_order = skeleton_day.stop_names if skeleton_day else []
        ordered: list[DraftActivity] = []
        for stop_name in stop_order:
            group = [
                activity
                for activity in selected
                if places[activity.evidence_place_id or ""].stop_name.casefold()
                == stop_name.casefold()
            ]
            ordered.extend(
                _nearest_neighbour(
                    group,
                    places,
                    anchors.get(stop_name.casefold()),
                )
            )
        ordered.extend(activity for activity in selected if activity not in ordered)
        for slot, activity in zip(place_slots, ordered, strict=True):
            day.activities[slot] = activity
    return updated


def _nearest_neighbour(
    activities: list[DraftActivity],
    places: dict[str, PlaceEvidence],
    anchor: GeoPoint | None,
) -> list[DraftActivity]:
    remaining = list(activities)
    ordered: list[DraftActivity] = []
    current = anchor or _centroid(
        [places[item.evidence_place_id or ""].location for item in remaining]
    )
    while remaining:
        next_activity = min(
            remaining,
            key=lambda item: _distance(
                current,
                places[item.evidence_place_id or ""].location,
            ),
        )
        ordered.append(next_activity)
        remaining.remove(next_activity)
        current = places[next_activity.evidence_place_id or ""].location or current
    return ordered


def _centroid(points: list[GeoPoint | None]) -> GeoPoint | None:
    available = [point for point in points if point is not None]
    if not available:
        return None
    return GeoPoint(
        latitude=sum(point.latitude for point in available) / len(available),
        longitude=sum(point.longitude for point in available) / len(available),
    )


def _distance(origin: GeoPoint | None, destination: GeoPoint | None) -> float:
    if origin is None or destination is None:
        return float("inf")
    latitude_1 = radians(origin.latitude)
    latitude_2 = radians(destination.latitude)
    latitude_delta = radians(destination.latitude - origin.latitude)
    longitude_delta = radians(destination.longitude - origin.longitude)
    value = (
        sin(latitude_delta / 2) ** 2
        + cos(latitude_1) * cos(latitude_2) * sin(longitude_delta / 2) ** 2
    )
    return 2 * 6371 * asin(sqrt(value))
