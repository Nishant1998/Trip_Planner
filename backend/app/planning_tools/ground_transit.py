"""Google Routes transit guidance; never ticket inventory or a booking quote."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx

from .contracts import GroundTransportInput, ToolResult, ToolStatus, TransportOption, TransportSegment
from .http import HttpTool, failure, unavailable


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration(value):
    try:
        return round(float(value.removesuffix("s"))) if isinstance(value, str) else None
    except ValueError:
        return None


def _fare(value):
    if not isinstance(value, dict) or not value.get("currencyCode"):
        return None, None
    try:
        return float(value.get("units", 0)) + int(value.get("nanos", 0)) / 1e9, value["currencyCode"]
    except (TypeError, ValueError):
        return None, None


def _requested_vehicle(vehicle: str, mode: str) -> bool:
    name = vehicle.upper()
    return ("TRAIN" in name or "RAIL" in name) if mode == "train" else "BUS" in name


class GoogleTransitProvider(HttpTool):
    def __init__(self, api_key: str | None, client: httpx.AsyncClient | None = None):
        super().__init__(client)
        self.api_key = api_key
        self.requests_made = 0

    async def search(self, query: GroundTransportInput) -> ToolResult[list[TransportOption]]:
        if not self.api_key:
            return unavailable("google_routes_transit", "Google Routes API key is not configured.")
        preference = ["TRAIN", "RAIL", "LIGHT_RAIL"] if query.mode == "train" else ["BUS"]
        body = {
            "origin": {"address": query.origin}, "destination": {"address": query.destination},
            "travelMode": "TRANSIT", "departureTime": query.departure_time.astimezone(UTC).isoformat(),
            "computeAlternativeRoutes": True,
            "transitPreferences": {"allowedTravelModes": preference},
        }
        fields = ",".join((
            "routes.duration", "routes.legs.steps.travelMode", "routes.legs.steps.transitDetails",
            "routes.travelAdvisory.transitFare",
        ))
        try:
            self.requests_made += 1
            response = await self.client.post(
                "https://routes.googleapis.com/directions/v2:computeRoutes", json=body,
                headers={"X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": fields}, timeout=30,
            )
            response.raise_for_status()
            routes = response.json().get("routes") or []
            if not isinstance(routes, list):
                raise ValueError("Expected route list")
            options = []
            for index, route in enumerate(routes[:10]):
                segments = []
                for leg in route.get("legs") or []:
                    for step in leg.get("steps") or []:
                        detail = step.get("transitDetails") or {}
                        stops = detail.get("stopDetails") or {}
                        line = detail.get("transitLine") or {}
                        vehicle = ((line.get("vehicle") or {}).get("type") or step.get("travelMode") or "UNKNOWN")
                        segments.append(TransportSegment(
                            mode=str(step.get("travelMode") or "UNKNOWN").lower(), vehicle=vehicle,
                            origin=(stops.get("departureStop") or {}).get("name"),
                            destination=(stops.get("arrivalStop") or {}).get("name"),
                            departure=_timestamp(stops.get("departureTime")),
                            arrival=_timestamp(stops.get("arrivalTime")),
                            carrier=(line.get("name") or line.get("nameShort")),
                        ))
                if not any(_requested_vehicle(segment.vehicle or "", query.mode) for segment in segments):
                    continue
                transit = [s for s in segments if s.mode == "transit"]
                departure = next((s.departure for s in transit if s.departure), None)
                arrival = next((s.arrival for s in reversed(transit) if s.arrival), None)
                amount, currency = _fare((route.get("travelAdvisory") or {}).get("transitFare"))
                warnings = ["Transit schedules and fares are guidance, not bookable tickets or passenger-group totals."]
                if amount is None:
                    warnings.append("Fare was not supplied by Google.")
                if departure is None or arrival is None:
                    warnings.append("A transit stop time was not supplied by Google.")
                options.append(TransportOption(
                    id=f"route-{index}", requested_mode=query.mode, origin=query.origin,
                    destination=query.destination, segments=segments, departure=departure,
                    arrival=arrival, duration_s=_duration(route.get("duration")),
                    fare_amount=amount, fare_currency=currency,
                    fare_classification="estimated" if amount is not None else "unknown",
                    source_url="https://www.google.com/maps/dir/?" + urlencode({
                        "api": 1, "origin": query.origin, "destination": query.destination,
                        "travelmode": "transit"}), warnings=warnings,
                ))
            if not options:
                return unavailable("google_routes_transit", f"No route containing {query.mode} was returned.", "no_route")
            return ToolResult(status=ToolStatus.PARTIAL, data=options,
                              provider="google_routes_transit", retrieved_at=datetime.now(UTC),
                              warnings=["Transit journeys are guidance only; verify schedules, accessibility and fares before travel."])
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as exc:
            return failure("google_routes_transit", exc)
