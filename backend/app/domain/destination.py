"""Deterministic destination fallback and resolver safeguards."""

from __future__ import annotations

from math import atan2, cos, radians, sin, sqrt

from app.models.evidence import EvidenceBundle, GeoPoint, PlaceEvidence
from app.models.context import (
    PlanningContext,
    ResolvedDestination,
    ResolvedStop,
)

NAMED_DESTINATIONS: dict[str, list[ResolvedStop]] = {
    "ashtavinayak": [
        ResolvedStop(
            name="Morgaon",
            country_code="IN",
            currency_code="INR",
            geocode_query=(
                "Shri Mayureshwar Ganapati Temple, Morgaon, Maharashtra, India"
            ),
        ),
        ResolvedStop(
            name="Siddhatek",
            country_code="IN",
            currency_code="INR",
            geocode_query=("Shri Siddhivinayak Temple, Siddhatek, Maharashtra, India"),
        ),
        ResolvedStop(
            name="Pali",
            country_code="IN",
            currency_code="INR",
            geocode_query=(
                "Shree Ballaleshwar Ganapati Temple, Pali, Maharashtra, India"
            ),
        ),
        ResolvedStop(
            name="Mahad",
            country_code="IN",
            currency_code="INR",
            geocode_query=(
                "Shree Varad Vinayak Temple, Mahad, Khalapur, Maharashtra, India"
            ),
        ),
        ResolvedStop(
            name="Theur",
            country_code="IN",
            currency_code="INR",
            geocode_query=(
                "Shree Chintamani Vinayaka Temple, Theur, Maharashtra, India"
            ),
        ),
        ResolvedStop(
            name="Lenyadri",
            country_code="IN",
            currency_code="INR",
            geocode_query=(
                "Shree Girijatmaja Ganapati Temple, Lenyadri, Maharashtra, India"
            ),
        ),
        ResolvedStop(
            name="Ozar",
            country_code="IN",
            currency_code="INR",
            geocode_query=("Shree Vighnahar Ganapati Temple, Ozar, Maharashtra, India"),
        ),
        ResolvedStop(
            name="Ranjangaon",
            country_code="IN",
            currency_code="INR",
            geocode_query=("Shree Mahaganapati Temple, Ranjangaon, Maharashtra, India"),
        ),
    ],
    "char dham": [
        ResolvedStop(
            name="Yamunotri", country_code="IN", currency_code="INR", suggested_days=2
        ),
        ResolvedStop(
            name="Gangotri", country_code="IN", currency_code="INR", suggested_days=2
        ),
        ResolvedStop(
            name="Kedarnath", country_code="IN", currency_code="INR", suggested_days=3
        ),
        ResolvedStop(
            name="Badrinath", country_code="IN", currency_code="INR", suggested_days=3
        ),
    ],
    "kashmir": [
        ResolvedStop(
            name="Srinagar",
            country_code="IN",
            currency_code="INR",
            suggested_days=3,
            geocode_query="Srinagar, Jammu and Kashmir, India",
        ),
        ResolvedStop(
            name="Gulmarg",
            country_code="IN",
            currency_code="INR",
            suggested_days=1,
            geocode_query="Gulmarg, Jammu and Kashmir, India",
        ),
        ResolvedStop(
            name="Pahalgam",
            country_code="IN",
            currency_code="INR",
            suggested_days=2,
            geocode_query="Pahalgam, Jammu and Kashmir, India",
        ),
    ],
    "north east": [
        ResolvedStop(
            name="Guwahati", country_code="IN", currency_code="INR", suggested_days=2
        ),
        ResolvedStop(
            name="Shillong", country_code="IN", currency_code="INR", suggested_days=2
        ),
        ResolvedStop(
            name="Cherrapunji", country_code="IN", currency_code="INR", suggested_days=1
        ),
        ResolvedStop(
            name="Kaziranga", country_code="IN", currency_code="INR", suggested_days=2
        ),
    ],
    "japan": [
        ResolvedStop(
            name="Tokyo",
            country_code="JP",
            currency_code="JPY",
            suggested_days=3,
            geocode_query="Tokyo, Japan",
        ),
        ResolvedStop(
            name="Kyoto",
            country_code="JP",
            currency_code="JPY",
            suggested_days=2,
            geocode_query="Kyoto, Japan",
        ),
        ResolvedStop(
            name="Osaka",
            country_code="JP",
            currency_code="JPY",
            suggested_days=2,
            geocode_query="Osaka, Japan",
        ),
    ],
}


def fallback_resolve_destination(context: PlanningContext) -> ResolvedDestination:
    """Resolve explicit or known named destinations without an LLM.

    This is the safe fallback when live destination geocoding is unavailable.
    """

    explicit_type = context.destination.type.casefold()
    supplied_items = [
        item.strip() for item in context.destination.items if item.strip()
    ]

    if explicit_type in {"city", "cities", "poi"}:
        stops = [
            ResolvedStop(
                name=item,
                currency_code=context.currency_code
                if context.trip_scope == "domestic"
                else None,
            )
            for item in supplied_items[:8]
        ]
        return ResolvedDestination(
            interpretation="Explicit destination order preserved.",
            stops=stops,
        )

    if context.resolved_city_names:
        stops = [
            ResolvedStop(
                name=name,
                currency_code=context.currency_code
                if context.trip_scope == "domestic"
                else None,
            )
            for name in context.resolved_city_names[:8]
        ]
        return ResolvedDestination(
            interpretation="Used destination cities already verified by Riya.",
            stops=stops,
            assumptions=[
                "Riya's resolved city list was treated as trusted destination evidence."
            ],
        )

    query = " ".join(supplied_items).casefold().strip()
    for name, known_stops in NAMED_DESTINATIONS.items():
        if name in query:
            return ResolvedDestination(
                interpretation=f"Resolved the validated {name.title()} destination.",
                stops=[stop.model_copy(deep=True) for stop in known_stops],
                assumptions=[
                    "Stop order was resolved automatically without further user communication."
                ],
            )

    return ResolvedDestination(
        interpretation="Used the validated destination labels as planning stops.",
        stops=[
            ResolvedStop(
                name=item,
                currency_code=context.currency_code
                if context.trip_scope == "domestic"
                else None,
            )
            for item in supplied_items[:8]
        ],
        assumptions=[
            "Broad destination expansion was unavailable; supplied labels were preserved."
        ],
    )


def reconcile_stop_coordinates(
    destination: ResolvedDestination,
    evidence: EvidenceBundle,
    selected_place_ids: list[str],
    *,
    maximum_distance_km: float = 50,
) -> tuple[ResolvedDestination, list[str]]:
    """Replace missing or clearly incorrect stop coordinates with exact POI evidence.

    The first selected provider-backed place for a stop is its planning anchor.
    Secondary POIs remain untouched and available to the planning stage.
    """

    place_map = evidence.place_map()
    selected_places: dict[str, list[PlaceEvidence]] = {}
    for place_id in selected_place_ids:
        place = place_map.get(place_id)
        if place is None or place.location is None:
            continue
        selected_places.setdefault(place.stop_name.casefold(), []).append(place)

    duplicate_coordinates = _duplicate_stop_coordinates(destination)
    corrected_stops: list[ResolvedStop] = []
    correction_source_ids: list[str] = []
    warnings: list[str] = []
    for stop in destination.stops:
        candidates = selected_places.get(stop.name.casefold(), [])
        if not candidates:
            corrected_stops.append(stop)
            continue

        anchor = candidates[0]
        assert anchor.location is not None
        current = _stop_point(stop)
        google_is_authoritative = _has_google_destination_source(destination, stop)
        should_replace = current is None or (
            not google_is_authoritative
            and (
                _coordinate_key(stop) in duplicate_coordinates
                or _distance_km(current, anchor.location) > maximum_distance_km
            )
        )
        if not should_replace:
            corrected_stops.append(stop)
            continue

        corrected_stops.append(
            stop.model_copy(
                update={
                    "latitude": anchor.location.latitude,
                    "longitude": anchor.location.longitude,
                }
            )
        )
        correction_source_ids.extend(anchor.source_ids)
        warnings.append(
            f"{stop.name}'s broad or unreliable geocoding result was replaced "
            f"with the verified location of {anchor.name}."
        )

    source_ids = list(dict.fromkeys([*destination.source_ids, *correction_source_ids]))
    return (
        destination.model_copy(
            update={"stops": corrected_stops, "source_ids": source_ids}
        ),
        warnings,
    )


def _duplicate_stop_coordinates(
    destination: ResolvedDestination,
) -> set[tuple[float, float]]:
    counts: dict[tuple[float, float], int] = {}
    for stop in destination.stops:
        key = _coordinate_key(stop)
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count > 1}


def _has_google_destination_source(
    destination: ResolvedDestination,
    stop: ResolvedStop,
) -> bool:
    prefix = f"src-google-destination-{_source_slug(stop.name)}-"
    return any(source_id.startswith(prefix) for source_id in destination.source_ids)


def _source_slug(value: str) -> str:
    return "".join(
        character
        for character in value.casefold()
        if character.isalnum() or character in "_-"
    )


def _coordinate_key(stop: ResolvedStop) -> tuple[float, float] | None:
    if stop.latitude is None or stop.longitude is None:
        return None
    return round(stop.latitude, 6), round(stop.longitude, 6)


def _stop_point(stop: ResolvedStop) -> GeoPoint | None:
    if stop.latitude is None or stop.longitude is None:
        return None
    return GeoPoint(latitude=stop.latitude, longitude=stop.longitude)


def _distance_km(origin: GeoPoint, destination: GeoPoint) -> float:
    latitude_1 = radians(origin.latitude)
    latitude_2 = radians(destination.latitude)
    latitude_delta = radians(destination.latitude - origin.latitude)
    longitude_delta = radians(destination.longitude - origin.longitude)
    value = (
        sin(latitude_delta / 2) ** 2
        + cos(latitude_1) * cos(latitude_2) * sin(longitude_delta / 2) ** 2
    )
    return 6371 * 2 * atan2(sqrt(value), sqrt(1 - value))
