"""Google-backed destination resolution for authoritative stop coordinates."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.models.evidence import GeoPoint, SourceRef
from app.models.context import (
    PlanningContext,
    ResolvedDestination,
    ResolvedStop,
)
from app.domain.destination import fallback_resolve_destination
from app.providers.http import ProviderHttpClient


class GoogleDestinationResolver:
    """Resolve planner stops with Google Places Text Search.

    OpenRouteService remains the route engine.  It is deliberately not used as
    the destination-coordinate authority because broad Pelias matches can map a
    small town or named circuit stop to a distant regional centroid.
    """

    SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
    FIELD_MASK = ",".join(
        (
            "places.id",
            "places.displayName",
            "places.formattedAddress",
            "places.location",
            "places.types",
            "places.primaryType",
            "places.businessStatus",
        )
    )

    def __init__(self, api_key: str, timeout_seconds: float = 20) -> None:
        self._api_key = api_key
        self._http = ProviderHttpClient(timeout_seconds)
        self._cache: dict[tuple[str, str], tuple[GeoPoint, str]] = {}
        self._sources: dict[str, SourceRef] = {}

    async def resolve(self, context: PlanningContext) -> ResolvedDestination:
        destination = fallback_resolve_destination(context)
        results = await asyncio.gather(
            *(self._resolve_stop(stop) for stop in destination.stops[:8]),
            return_exceptions=True,
        )

        resolved_stops: list[ResolvedStop] = []
        source_ids: list[str] = list(destination.source_ids)
        unresolved: list[str] = []
        for stop, result in zip(destination.stops[:8], results, strict=True):
            if isinstance(result, BaseException):
                unresolved.append(stop.name)
                resolved_stops.append(stop)
                continue
            point, source_id = result
            resolved_stops.append(
                stop.model_copy(
                    update={
                        "latitude": point.latitude,
                        "longitude": point.longitude,
                    }
                )
            )
            source_ids.append(source_id)

        assumptions = list(destination.assumptions)
        if unresolved:
            assumptions.append(
                "Google could not resolve these confirmed stops; their names were "
                "preserved for provider-backed POI anchoring: " + ", ".join(unresolved)
            )
        return destination.model_copy(
            update={
                "stops": resolved_stops,
                "assumptions": assumptions,
                "source_ids": list(dict.fromkeys(source_ids)),
            }
        )

    async def geocode(
        self,
        name: str,
        country_code: str | None = None,
    ) -> GeoPoint:
        point, _ = await self._search(name, country_code, stop_name=name)
        return point

    async def _resolve_stop(self, stop: ResolvedStop) -> tuple[GeoPoint, str]:
        return await self._search(
            stop.geocode_query or stop.name,
            stop.country_code,
            stop_name=stop.name,
        )

    async def _search(
        self,
        query: str,
        country_code: str | None,
        *,
        stop_name: str,
    ) -> tuple[GeoPoint, str]:
        key = (query.casefold(), (country_code or "").upper())
        if key in self._cache:
            return self._cache[key]

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
                "maxResultCount": 5,
                **({"regionCode": country_code} if country_code else {}),
            },
        )
        record = _first_located_result(response.json().get("places") or [])
        place_id = str(record.get("id") or "").strip()
        location = record.get("location") or {}
        point = GeoPoint(
            latitude=float(location["latitude"]),
            longitude=float(location["longitude"]),
        )
        source_id = (
            f"src-google-destination-{_safe_id(stop_name.casefold())}-"
            f"{_safe_id(place_id)}"
        )
        title = str((record.get("displayName") or {}).get("text") or stop_name)
        self._sources[source_id] = SourceRef(
            id=source_id,
            provider="google-places",
            kind="destination-geocoding",
            title=f"Google destination: {title}",
            url=None,
            provider_record_id=place_id,
            retrieved_at=datetime.now(UTC),
        )
        result = (point, source_id)
        self._cache[key] = result
        return result

    def sources(self) -> list[SourceRef]:
        return list(self._sources.values())

    async def close(self) -> None:
        await self._http.close()


def _first_located_result(records: list[dict[str, Any]]) -> dict[str, Any]:
    for record in records:
        location = record.get("location") or {}
        record_types = {record.get("primaryType"), *(record.get("types") or [])}
        if (
            record.get("businessStatus") in (None, "OPERATIONAL")
            and record.get("id")
            and location.get("latitude") is not None
            and location.get("longitude") is not None
            and not record_types & _NON_VISITABLE_TYPES
        ):
            return record
    raise LookupError("Google Places returned no located destination result.")


_NON_VISITABLE_TYPES = {
    "intersection",
    "postal_code",
    "route",
    "street_address",
}


def _safe_id(value: str) -> str:
    return "".join(
        character for character in value if character.isalnum() or character in "_-"
    )
