"""Google Routes matrix and directions for itinerary travel-time evidence."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from .contracts import MapLookupInput, MatrixInput, RouteInput, RouteLeg, ToolResult, ToolStatus
from .http import HttpTool, failure, result, unavailable

BASE = "https://routes.googleapis.com/directions/v2"
MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"


def _waypoint(point):
    return {"waypoint": {"location": {"latLng": {
        "latitude": point.location.latitude, "longitude": point.location.longitude,
    }}}}


def _seconds(raw: str | None) -> int | None:
    if not raw:
        return None
    return round(float(raw.removesuffix("s")))


class RoutesTools(HttpTool):
    def __init__(self, api_key: str | None, client: httpx.AsyncClient | None = None):
        super().__init__(client)
        self.api_key = api_key

    def _headers(self, fields: str) -> dict[str, str]:
        return {"X-Goog-Api-Key": self.api_key or "", "X-Goog-FieldMask": fields}

    async def lookup(self, query: MapLookupInput):
        """One agent-facing Maps tool with matrix and directions modes."""
        query = MapLookupInput.model_validate(query)
        if query.action == "matrix":
            return await self.matrix(MatrixInput(
                origins=query.origins, destinations=query.destinations,
                mode=query.mode, departure_time=query.departure_time,
            ))
        return await self.directions(RouteInput(
            origin=query.origin, destination=query.destination,
            mode=query.mode, departure_time=query.departure_time,
        ))

    async def matrix(self, query: MatrixInput):
        if not self.api_key:
            return unavailable("google_routes", "Google Routes API key is not configured.")
        # Google caps ordinary matrices at 100 elements. Split by origin, preserve
        # the requested pair order, and never retry a successful/billable chunk.
        chunk_size = max(1, 100 // len(query.destinations))
        pair_results: dict[tuple[int, int], RouteLeg] = {}
        failed_chunks = 0
        first_failure_code: str | None = None
        for offset in range(0, len(query.origins), chunk_size):
            origins = query.origins[offset:offset + chunk_size]
            body = {
                "origins": [_waypoint(point) for point in origins],
                "destinations": [_waypoint(point) for point in query.destinations],
                "travelMode": query.mode,
            }
            if query.departure_time:
                body["departureTime"] = query.departure_time.isoformat()
            try:
                response = await self.client.post(
                    MATRIX_URL, json=body,
                    headers=self._headers("originIndex,destinationIndex,distanceMeters,duration,status,condition"),
                )
                response.raise_for_status()
                elements = response.json()
                if not isinstance(elements, list):
                    raise ValueError("Expected matrix element list")
                for element in elements:
                    i, j = element["originIndex"], element["destinationIndex"]
                    if not (0 <= i < len(origins) and 0 <= j < len(query.destinations)):
                        raise ValueError("Matrix index outside request")
                    status = element.get("status") or {}
                    has_error = isinstance(status, dict) and status.get("code", 0) != 0
                    route_exists = not has_error and element.get("condition") in (None, "ROUTE_EXISTS")
                    duration = _seconds(element.get("duration")) if route_exists else None
                    distance = element.get("distanceMeters") if route_exists else None
                    leg_status = (ToolStatus.OK if distance is not None and duration is not None else
                                  ToolStatus.PARTIAL if distance is not None or duration is not None else
                                  ToolStatus.UNAVAILABLE)
                    pair_results[offset + i, j] = RouteLeg(
                        origin_id=origins[i].id, destination_id=query.destinations[j].id,
                        mode=query.mode, distance_m=distance, duration_s=duration,
                        status=leg_status,
                        warning=(None if leg_status == ToolStatus.OK else
                                 "Route distance or duration is missing." if leg_status == ToolStatus.PARTIAL else
                                 "No usable route returned for this pair."),
                    )
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                failed_chunks += 1
                first_failure_code = first_failure_code or failure("google_routes", exc).error_code
        legs = [pair_results.get((i, j)) or RouteLeg(
            origin_id=origin.id, destination_id=destination.id, mode=query.mode,
            status=ToolStatus.UNAVAILABLE, warning="No route returned for this pair.",
        ) for i, origin in enumerate(query.origins)
          for j, destination in enumerate(query.destinations)]
        warnings = []
        if failed_chunks:
            warnings.append(f"{failed_chunks} route matrix request batch(es) failed.")
        if any(leg.status != ToolStatus.OK for leg in legs):
            warnings.append("Some route pairs are partial or unavailable.")
        status = (ToolStatus.ERROR if failed_chunks and not pair_results else
                  ToolStatus.PARTIAL if warnings else ToolStatus.OK)
        return ToolResult(status=status, data=legs, provider="google_routes",
                          retrieved_at=datetime.now(UTC), warnings=warnings,
                          error_code=first_failure_code if status == ToolStatus.ERROR else None)

    async def directions(self, query: RouteInput):
        if not self.api_key:
            return unavailable("google_routes", "Google Routes API key is not configured.")
        body = {
            "origin": _waypoint(query.origin)["waypoint"],
            "destination": _waypoint(query.destination)["waypoint"],
            "travelMode": query.mode,
        }
        if query.departure_time:
            body["departureTime"] = query.departure_time.isoformat()
        try:
            response = await self.client.post(
                f"{BASE}:computeRoutes", json=body,
                headers=self._headers("routes.distanceMeters,routes.duration,routes.polyline.encodedPolyline"),
            )
            response.raise_for_status()
            routes = response.json().get("routes") or []
            if not routes:
                return unavailable("google_routes", "No route was returned.", "no_route")
            route = routes[0]
            distance = route.get("distanceMeters")
            duration = _seconds(route.get("duration"))
            if distance is None and duration is None:
                return unavailable("google_routes", "No usable route was returned.", "no_route")
            partial = distance is None or duration is None
            return result(RouteLeg(
                origin_id=query.origin.id, destination_id=query.destination.id,
                mode=query.mode, distance_m=distance, duration_s=duration,
                polyline=(route.get("polyline") or {}).get("encodedPolyline"),
                status=ToolStatus.PARTIAL if partial else ToolStatus.OK,
                warning="Route distance or duration is missing." if partial else None,
            ), "google_routes", warnings=["Route distance or duration is missing."] if partial else None)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            return failure("google_routes", exc)
