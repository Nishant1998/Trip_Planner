"""Google Places adapter for exact POI identity and published opening hours."""

from __future__ import annotations

from datetime import UTC, datetime, time
from math import atan2, cos, radians, sin, sqrt
from typing import Any
from urllib.parse import urlencode

from app.models.evidence import (
    FactStatus,
    GeoPoint,
    PlaceEvidence,
    SourceRef,
    WeeklyOpeningPeriod,
)
from app.models.context import PlanningContext, ResolvedStop
from app.providers.http import ProviderHttpClient


class GooglePlacesProvider:
    SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
    FIELD_MASK = ",".join(
        (
            "places.id",
            "places.displayName",
            "places.formattedAddress",
            "places.location",
            "places.types",
            "places.primaryType",
            "places.regularOpeningHours",
            "places.googleMapsUri",
            "places.businessStatus",
        )
    )

    def __init__(self, api_key: str, timeout_seconds: float = 20) -> None:
        self._api_key = api_key
        self._http = ProviderHttpClient(timeout_seconds)
        self._sources: dict[str, SourceRef] = {}

    async def search_places(
        self,
        stop: ResolvedStop,
        context: PlanningContext,
    ) -> list[PlaceEvidence]:
        records: dict[str, dict[str, Any]] = {}
        primary_place_ids: set[str] = set()
        queries = _search_queries(stop, context)
        for query_index, query in enumerate(queries):
            response = await self._http.post(
                self.SEARCH_URL,
                headers={
                    "X-Goog-Api-Key": self._api_key,
                    "X-Goog-FieldMask": self.FIELD_MASK,
                    "Content-Type": "application/json",
                },
                json={
                    "textQuery": query,
                    "languageCode": "en",
                    "maxResultCount": 20,
                    **({"regionCode": stop.country_code} if stop.country_code else {}),
                    **_location_bias(stop),
                },
            )
            query_records = response.json().get("places") or []
            if query_index == 0:
                primary = _primary_destination_record(query_records)
                if primary is not None:
                    primary_place_ids.add(str(primary["id"]))
            for record in query_records:
                place_id = str(record.get("id") or "")
                if place_id:
                    records.setdefault(place_id, record)

        places = [
            normalized
            for record in records.values()
            if (
                normalized := self._normalize(
                    record,
                    stop,
                    is_primary_destination=(
                        str(record.get("id") or "") in primary_place_ids
                    ),
                )
            )
            is not None
            and _inside_stop_area(normalized, stop, bool(primary_place_ids))
        ]
        return _rank_places(places, context)[:16]

    def _normalize(
        self,
        record: dict[str, Any],
        stop: ResolvedStop,
        *,
        is_primary_destination: bool = False,
    ) -> PlaceEvidence | None:
        if record.get("businessStatus") not in (None, "OPERATIONAL"):
            return None
        record_types = {record.get("primaryType"), *(record.get("types") or [])}
        if record_types & _NON_VISITABLE_TYPES:
            return None
        place_id = str(record.get("id") or "").strip()
        name = str((record.get("displayName") or {}).get("text") or "").strip()
        location = record.get("location") or {}
        if not place_id or not name:
            return None

        hours = record.get("regularOpeningHours") or {}
        periods = [
            period
            for raw in hours.get("periods") or []
            if (period := _opening_period(raw)) is not None
        ]
        source_id = f"src-google-place-{_safe_id(place_id)}"
        maps_url = _maps_url(name, str(record.get("formattedAddress") or ""), place_id)
        self._sources[source_id] = SourceRef(
            id=source_id,
            provider="google-places",
            kind="place",
            title=f"Google Places: {name}",
            url=maps_url,
            provider_record_id=place_id,
            retrieved_at=datetime.now(UTC),
        )
        latitude = location.get("latitude")
        longitude = location.get("longitude")
        categories = [
            str(value).replace("_", " ")
            for value in [record.get("primaryType"), *(record.get("types") or [])]
            if value
        ]
        return PlaceEvidence(
            # A nearby temple can be returned for more than one resolved stop.
            # Keep its Google ID in ``provider_place_id``, but make the
            # planner's own evidence ID stop-scoped so one record cannot
            # overwrite another during final validation.
            id=_evidence_id(stop.name, place_id),
            provider_place_id=place_id,
            name=name,
            stop_name=stop.name,
            categories=list(dict.fromkeys(categories)),
            address=record.get("formattedAddress"),
            location=(
                GeoPoint(latitude=float(latitude), longitude=float(longitude))
                if latitude is not None and longitude is not None
                else None
            ),
            google_maps_uri=record.get("googleMapsUri"),
            estimated_visit_minutes=_visit_minutes(categories),
            opening_hours="; ".join(hours.get("weekdayDescriptions") or []) or None,
            weekday_hours=list(hours.get("weekdayDescriptions") or []),
            opening_periods=periods,
            status=FactStatus.VERIFIED,
            source_ids=[source_id],
            is_primary_destination=is_primary_destination,
        )

    def sources(self) -> list[SourceRef]:
        return list(self._sources.values())

    async def close(self) -> None:
        await self._http.close()


def _search_queries(stop: ResolvedStop, context: PlanningContext) -> list[str]:
    location = stop.geocode_query or stop.name
    # The exact confirmed stop is always searched first. This ensures a named
    # temple, museum, monument, or other requested POI cannot disappear from a
    # broader "tourist attractions" result set.
    queries = [location]
    for place in context.must_see:
        # Keep the user wording, then add a practical landmark query for
        # temples/monuments whose short devotional name is ambiguous.
        queries.append(f"{place} in {location}")
        if any(
            word in place.casefold()
            for word in ("temple", "vinayak", "ganapati", "shrine")
        ):
            queries.append(f"{place} temple location address")

    # A region has several distinct tourist areas.  Ask focused questions so
    # the ranking agent receives a varied, usable candidate set instead of a
    # single generic list for the whole state or region.
    for interest in context.activity_interests[:4]:
        queries.append(f"{interest} attractions in {location}")
    if context.accessibility_needs:
        queries.append(f"accessible family attractions in {location}")
    if context.cuisine_preferences:
        queries.append(f"local food markets and dining areas in {location}")
    queries.append(f"top tourist attractions in {location}")
    return list(dict.fromkeys(queries))


def _location_bias(stop: ResolvedStop) -> dict[str, Any]:
    if stop.latitude is None or stop.longitude is None:
        return {}
    return {
        "locationBias": {
            "circle": {
                "center": {
                    "latitude": stop.latitude,
                    "longitude": stop.longitude,
                },
                "radius": 50_000,
            }
        }
    }


def _primary_destination_record(
    records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return an exact POI match, but never mark a city/region as an activity."""

    locality_types = {
        "administrative_area_level_1",
        "administrative_area_level_2",
        "administrative_area_level_3",
        "country",
        "locality",
        "postal_code",
        "sublocality",
    }
    for record in records:
        location = record.get("location") or {}
        record_types = {record.get("primaryType"), *(record.get("types") or [])}
        if (
            record.get("businessStatus") in (None, "OPERATIONAL")
            and record.get("id")
            and location.get("latitude") is not None
            and location.get("longitude") is not None
        ):
            if record_types & locality_types:
                return None
            if record_types & _NON_VISITABLE_TYPES:
                continue
            return record
    return None


_NON_VISITABLE_TYPES = {
    "intersection",
    "postal_code",
    "route",
    "street_address",
}


def _inside_stop_area(
    place: PlaceEvidence,
    stop: ResolvedStop,
    has_exact_primary: bool,
) -> bool:
    if place.is_primary_destination:
        return True
    if place.location is None or stop.latitude is None or stop.longitude is None:
        return True
    # Named POI stops use a tighter local area. City/region stops retain a
    # wider area so normal day trips are not lost.
    maximum_distance_km = 35 if has_exact_primary else 75
    return (
        _distance_km(
            stop.latitude,
            stop.longitude,
            place.location.latitude,
            place.location.longitude,
        )
        <= maximum_distance_km
    )


def _distance_km(
    latitude_1: float,
    longitude_1: float,
    latitude_2: float,
    longitude_2: float,
) -> float:
    first_latitude = radians(latitude_1)
    second_latitude = radians(latitude_2)
    latitude_delta = radians(latitude_2 - latitude_1)
    longitude_delta = radians(longitude_2 - longitude_1)
    value = (
        sin(latitude_delta / 2) ** 2
        + cos(first_latitude) * cos(second_latitude) * sin(longitude_delta / 2) ** 2
    )
    return 6371 * 2 * atan2(sqrt(value), sqrt(1 - value))


def _opening_period(raw: dict[str, Any]) -> WeeklyOpeningPeriod | None:
    opening = raw.get("open") or {}
    if "day" not in opening:
        return None
    closing = raw.get("close") or {}
    return WeeklyOpeningPeriod(
        open_day=_python_weekday(int(opening["day"])),
        open_time=_provider_time(opening),
        close_day=(_python_weekday(int(closing["day"])) if "day" in closing else None),
        close_time=_provider_time(closing) if "day" in closing else None,
    )


def _provider_time(value: dict[str, Any]) -> time:
    return time(hour=int(value.get("hour", 0)), minute=int(value.get("minute", 0)))


def _python_weekday(google_day: int) -> int:
    """Google uses Sunday=0; Python uses Monday=0."""
    return (google_day + 6) % 7


def _maps_url(name: str, address: str, place_id: str) -> str:
    query = ", ".join(value for value in (name, address) if value)
    return "https://www.google.com/maps/search/?" + urlencode(
        {"api": "1", "query": query, "query_place_id": place_id}
    )


def _rank_places(
    places: list[PlaceEvidence],
    context: PlanningContext,
) -> list[PlaceEvidence]:
    wanted = [*context.must_see, *context.activity_interests]
    avoided = [value.casefold() for value in context.avoid]

    def score(place: PlaceEvidence) -> tuple[int, str]:
        text = f"{place.name} {' '.join(place.categories)}".casefold()
        if any(value in text for value in avoided):
            return (-1000, place.name)
        matches = sum(value.casefold() in text for value in wanted)
        identity_bonus = 5 if place.provider_place_id else 0
        hours_bonus = 3 if place.opening_periods else 0
        primary_bonus = 10_000 if place.is_primary_destination else 0
        return (
            primary_bonus + matches * 20 + identity_bonus + hours_bonus,
            place.name,
        )

    return [
        place
        for place in sorted(places, key=score, reverse=True)
        if score(place)[0] >= 0
    ]


def _visit_minutes(categories: list[str]) -> int:
    text = " ".join(categories).casefold()
    if any(value in text for value in ("museum", "amusement park", "zoo")):
        return 150
    if any(value in text for value in ("park", "garden", "temple", "church")):
        return 90
    return 105


def _safe_id(value: str) -> str:
    return "".join(
        character for character in value if character.isalnum() or character in "_-"
    )


def _evidence_id(stop_name: str, provider_place_id: str) -> str:
    """Build a readable, stop-scoped internal key for one Google place."""

    return (
        f"google-place-{_safe_id(stop_name.casefold())}-{_safe_id(provider_place_id)}"
    )
