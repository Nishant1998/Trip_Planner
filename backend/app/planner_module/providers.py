"""Provider protocol and real HTTP provider implementation."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

import httpx

from app.services.provider_usage import HTTP_HOOKS

logger = logging.getLogger(__name__)


class ResearchProvider(Protocol):
    async def web_search(self, query: str) -> list[dict[str, Any]]: ...
    async def resolve_destination(
        self, destination_type: str, item: str, subtype: str | None
    ) -> list[dict[str, Any]]: ...
    async def discover_pois(
        self, stop: dict[str, Any], query: str
    ) -> list[dict[str, Any]]: ...
    async def search_places(
        self,
        query: str,
        max_results: int = 10,
        *,
        location_bias: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...
    async def place_details(self, poi: dict[str, Any]) -> dict[str, Any]: ...
    async def route(
        self, origin: dict[str, Any], destination: dict[str, Any], mode: str
    ) -> dict[str, Any] | None: ...
    async def weather(
        self, stop: dict[str, Any], start_date: str, end_date: str
    ) -> dict[str, Any]: ...
    async def currency(self, base: str, quote: str) -> dict[str, Any]: ...
    def supports_route(self, mode: str) -> bool: ...


class Provider:
    """Real provider adapter. It returns raw factual provider fields, never guessed values."""

    def __init__(
        self,
        *,
        google_api_key: str,
        tavily_api_key: str | None,
        ors_api_key: str | None,
        ors_base_url: str | None = None,
    ) -> None:
        self.google_api_key = google_api_key
        self.tavily_api_key = tavily_api_key
        self.ors_api_key = ors_api_key
        self.ors_base_url = (
            ors_base_url
            or os.getenv("ORS_BASE_URL", "https://api.heigit.org/openrouteservice")
        ).rstrip("/")
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(
                float(os.getenv("RESEARCH_HTTP_TIMEOUT_SECONDS", "30"))
            ),
            trust_env=False,
            event_hooks=HTTP_HOOKS,
        )
        self.provider_metrics: dict[str, int] = {}
        # Deliberately excludes URLs, queries, headers, and request bodies so a
        # debug run can be audited without leaking keys or user-entered text.
        self.call_log: list[dict[str, str | int]] = []
        self.request_limits: dict[str, int] = {}

    def configure_limits(self, limits: Any) -> None:
        """Physical HTTP ceilings, supplied by the graph's one configuration object."""
        self.request_limits = {
            "google_text_search": limits.max_google_searches,
            "google_place_details": limits.max_place_details_calls,
            "web_search": limits.max_web_searches,
            "routes": limits.max_route_calls,
            "weather": limits.max_weather_requests,
            "currency": limits.max_currency_requests,
        }

    def _count(self, name: str) -> None:
        next_count = self.provider_metrics.get(name, 0) + 1
        maximum = self.request_limits.get(name)
        if maximum is not None and next_count > maximum:
            logger.warning(
                "physical_budget_exhausted",
                extra={"provider": name, "count": next_count - 1},
            )
            raise RuntimeError(
                f"Actual provider budget exhausted for {name}: {maximum} requests"
            )
        self.provider_metrics[name] = next_count
        logger.debug("provider_request", extra={"provider": name, "count": next_count})
        self.call_log.append(
            {
                "provider_call": name,
                "sequence": next_count,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            }
        )

    @classmethod
    def from_environment(cls) -> Provider:
        google = (
            os.getenv("GOOGLE_MAPS_API_KEY")
            or os.getenv("GPLACES_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
        )
        if not google:
            raise RuntimeError(
                "Live mode requires GOOGLE_MAPS_API_KEY (or GPLACES_API_KEY)."
            )
        return cls(
            google_api_key=google,
            tavily_api_key=os.getenv("TAVILY_API_KEY"),
            ors_api_key=os.getenv("ORS_API_KEY"),
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def _google_search(
        self,
        query: str,
        count: int = 10,
        *,
        location_bias: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self._count("google_text_search")
        response = await self.http.post(
            "https://places.googleapis.com/v1/places:searchText",
            headers={
                "X-Goog-Api-Key": self.google_api_key,
                "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress,places.location,places.types,places.primaryType,places.googleMapsUri",
            },
            json={
                "textQuery": query,
                "pageSize": count,
                **({"locationBias": {"circle": {"center": location_bias, "radius": 50000.0}}} if location_bias else {}),
            },
        )
        response.raise_for_status()
        return response.json().get("places", [])

    @staticmethod
    def _record(place: dict[str, Any], kind: str) -> dict[str, Any]:
        location = place.get("location") or {}
        place_id = str(place.get("id", "")).removeprefix("places/")
        return {
            "id": "place_" + hashlib.sha256(place_id.encode()).hexdigest()[:12],
            "google_place_id": place_id,
            "name": (place.get("displayName") or {}).get("text", "Unknown place"),
            "type": place.get("primaryType") or kind,
            "coordinates": {
                "latitude": location["latitude"],
                "longitude": location["longitude"],
            },
            "address": place.get("formattedAddress"),
            "categories": place.get("types", []),
            "google_maps_uri": place.get("googleMapsUri"),
            "provider": "google_places",
        }

    async def web_search(self, query: str) -> list[dict[str, Any]]:
        """Return Tavily source snippets; semantic interpretation belongs to the graph."""
        if not self.tavily_api_key:
            return []
        self._count("web_search")
        response = await self.http.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {self.tavily_api_key}"},
            json={
                "query": query,
                "search_depth": "advanced",
                "max_results": 5,
                "include_raw_content": "text",
            },
        )
        response.raise_for_status()
        return response.json().get("results", [])

    async def resolve_destination(
        self, destination_type: str, item: str, subtype: str | None
    ) -> list[dict[str, Any]]:
        places = await self.search_places(item, 1)
        records = []
        if places and (places[0].get("coordinates") or {}).get("latitude") is not None:
            record = dict(places[0])
            record["type"] = destination_type
            record["rationale"] = "Google Places resolved the requested destination."
            record["confidence"] = "high"
            records.append(record)
        return records

    async def search_places(
        self,
        query: str,
        max_results: int = 10,
        *,
        location_bias: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Neutral Google Places lookup; callers assign no trip semantic role."""
        places = await self._google_search(
            query, max_results, location_bias=location_bias
        )
        return [
            self._record(place, "place")
            for place in places
            if (place.get("location") or {}).get("latitude") is not None
        ]

    async def discover_pois(
        self, stop: dict[str, Any], query: str
    ) -> list[dict[str, Any]]:
        return await self.search_places(f"{query} in {stop['name']}", 20)

    async def place_details(self, poi: dict[str, Any]) -> dict[str, Any]:
        self._count("google_place_details")
        response = await self.http.get(
            f"https://places.googleapis.com/v1/places/{poi['google_place_id']}",
            headers={
                "X-Goog-Api-Key": self.google_api_key,
                "X-Goog-FieldMask": "id,displayName,formattedAddress,location,types,primaryType,regularOpeningHours,businessStatus,googleMapsUri,accessibilityOptions",
            },
        )
        response.raise_for_status()
        raw = response.json()
        record = dict(poi)
        opening = raw.get("regularOpeningHours") or {}
        accessibility = raw.get("accessibilityOptions")
        business_status = raw.get("businessStatus")
        record.update(
            {
                "name": (raw.get("displayName") or {}).get("text", poi["name"]),
                "address": raw.get("formattedAddress", poi.get("address")),
                "coordinates": raw.get("location", poi["coordinates"]),
                "categories": raw.get("types", poi.get("categories", [])),
                "type": raw.get("primaryType", poi.get("type", "place")),
                "google_maps_uri": raw.get("googleMapsUri", poi.get("google_maps_uri")),
                "business_status": business_status,
                "is_recommendable": business_status != "CLOSED_PERMANENTLY",
                "opening_hours": opening.get("weekdayDescriptions", []),
                "opening_periods": opening.get("periods", []),
                "visit_minutes": None,
                "visit_duration_basis": "unknown",
                "adult_entry_cost": None,
                "entry_currency": None,
                "reservation_required": None,
                "accessibility_notes": [
                    f"{key}={value}"
                    for key, value in (accessibility or {}).items()
                    if value is not None
                ],
                "restrictions": [],
                "indoor_outdoor": "unknown",
                "unresolved_fields": [
                    "visit_duration",
                    "adult_entry_cost",
                    "reservation_rules",
                    *([] if accessibility is not None else ["accessibility"]),
                ],
                "provider": "google_place_details",
            }
        )
        return record

    async def route(
        self, origin: dict[str, Any], destination: dict[str, Any], mode: str
    ) -> dict[str, Any] | None:
        if not self.ors_api_key or mode not in {"driving", "walking", "cycling"}:
            return None
        profile = {
            "driving": "driving-car",
            "walking": "foot-walking",
            "cycling": "cycling-regular",
        }[mode]
        self._count("routes")
        start, end = origin["coordinates"], destination["coordinates"]
        response = await self.http.get(
            f"{self.ors_base_url}/v2/directions/{profile}",
            headers={"Authorization": self.ors_api_key},
            params={
                "start": f"{start['longitude']},{start['latitude']}",
                "end": f"{end['longitude']},{end['latitude']}",
            },
        )
        response.raise_for_status()
        summary = (
            (response.json().get("features") or [{}])[0].get("properties") or {}
        ).get("summary") or {}
        return {
            "distance_km": round(float(summary["distance"]) / 1000, 2),
            "duration_minutes": round(float(summary["duration"]) / 60),
            "mode": mode,
            "provider": "openrouteservice",
        }

    def supports_route(self, mode: str) -> bool:
        """ORS has road profiles only; it is not a public-transit/schedule provider."""
        return bool(self.ors_api_key) and mode in {"driving", "walking", "cycling"}

    async def weather(
        self, stop: dict[str, Any], start_date: str, end_date: str
    ) -> dict[str, Any]:
        """Use historical seasonal context outside the short forecast window."""
        self._count("weather")
        coordinates = stop["coordinates"]
        requested_start = date.fromisoformat(start_date)
        requested_end = date.fromisoformat(end_date)
        today = datetime.now(timezone.utc).date()
        if requested_start > today + timedelta(
            days=16
        ) or requested_end < today - timedelta(days=1):
            historical_start = requested_start.replace(year=requested_start.year - 1)
            historical_end = requested_end.replace(year=requested_end.year - 1)
            response = await self.http.get(
                "https://archive-api.open-meteo.com/v1/archive",
                params={
                    "latitude": coordinates["latitude"],
                    "longitude": coordinates["longitude"],
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
                    "timezone": "auto",
                    "start_date": historical_start.isoformat(),
                    "end_date": historical_end.isoformat(),
                },
            )
            response.raise_for_status()
            return {
                "kind": "seasonal",
                "summary": f"Historical seasonal context for {historical_start} to {historical_end}: "
                + json.dumps(response.json().get("daily", {})),
                "provider": "open_meteo_archive",
            }
        response = await self.http.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": coordinates["latitude"],
                "longitude": coordinates["longitude"],
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "auto",
                "start_date": start_date,
                "end_date": end_date,
            },
        )
        if response.status_code == 400:
            return {
                "kind": "unknown",
                "summary": "Exact forecast is unavailable for these dates; seasonal research still needed.",
                "provider": "open_meteo",
            }
        response.raise_for_status()
        return {
            "kind": "forecast",
            "summary": json.dumps(response.json().get("daily", {})),
            "provider": "open_meteo",
        }

    async def currency(self, base: str, quote: str) -> dict[str, Any]:
        self._count("currency")
        response = await self.http.get(
            f"{os.getenv('FRANKFURTER_BASE_URL', 'https://api.frankfurter.dev/v1').rstrip('/')}/latest",
            params={"from": base, "to": quote},
        )
        response.raise_for_status()
        return {
            "rate": response.json().get("rates", {}).get(quote),
            "provider": "frankfurter",
        }
