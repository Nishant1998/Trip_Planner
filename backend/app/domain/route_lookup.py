"""Small cached route boundary used inside the Planner Agent's tool."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.evidence import GeoPoint, RouteEvidence
from app.models.route import RouteMatrix, RouteMatrixLocation
from app.providers.base import RouteProvider


@dataclass(frozen=True, slots=True)
class RoutePoint:
    label: str
    location: GeoPoint | None = None
    # A provider-facing lookup phrase.  This is intentionally separate from
    # ``label`` so the itinerary can display "Baga Beach" while a geocoder
    # receives the unambiguous "Baga Beach, Goa".
    query: str | None = None


@dataclass(slots=True)
class CachedRouteLookup:
    """Cache provider routes for one request; no cross-user state is retained."""

    provider: RouteProvider
    routes: dict[tuple[str, str, str], RouteEvidence] = field(default_factory=dict)
    matrices: dict[tuple[tuple[str, ...], str], RouteMatrix] = field(
        default_factory=dict
    )

    async def get(
        self,
        origin: RoutePoint,
        destination: RoutePoint,
        mode: str,
    ) -> RouteEvidence:
        key = (
            origin.label.casefold(),
            destination.label.casefold(),
            mode.casefold(),
        )
        if key not in self.routes:
            route = await self.provider.get_route(
                origin.query or origin.label,
                destination.query or destination.label,
                mode,
                origin_location=origin.location,
                destination_location=destination.location,
            )
            # Provider evidence is stored against the stable itinerary labels,
            # not its geocoding helper phrases.  This keeps route matching,
            # auditing, and UI output consistent.
            self.routes[key] = route.model_copy(
                update={
                    "origin_name": origin.label,
                    "destination_name": destination.label,
                }
            )
        return self.routes[key]

    async def get_matrix(
        self,
        locations: list[RoutePoint],
        mode: str,
    ) -> RouteMatrix:
        """Fetch one provider matrix, falling back to cached pair routes if needed."""

        key = (
            tuple(
                f"{point.label.casefold()}:{point.location.latitude if point.location else ''}:"
                f"{point.location.longitude if point.location else ''}"
                for point in locations
            ),
            mode.casefold(),
        )
        if key in self.matrices:
            return self.matrices[key]
        provider_method = getattr(self.provider, "get_route_matrix", None)
        if provider_method is not None:
            try:
                matrix = await provider_method(
                    [
                        RouteMatrixLocation(label=point.label, location=point.location)
                        for point in locations
                    ],
                    mode,
                )
                self.matrices[key] = matrix
                return matrix
            except Exception:
                # A provider matrix failure must not stop planning. Pair routes retain
                # exactly the same route evidence contract, although less efficiently.
                pass

        size = len(locations)
        durations: list[list[int | None]] = [[None] * size for _ in range(size)]
        distances: list[list[float | None]] = [[None] * size for _ in range(size)]
        source_ids: set[str] = set()
        for origin_index, origin in enumerate(locations):
            durations[origin_index][origin_index] = 0
            distances[origin_index][origin_index] = 0.0
            for destination_index, destination in enumerate(locations):
                if origin_index == destination_index:
                    continue
                route = await self.get(origin, destination, mode)
                durations[origin_index][destination_index] = route.duration_minutes
                distances[origin_index][destination_index] = route.distance_km
                source_ids.update(route.source_ids)
        matrix = RouteMatrix(
            labels=[point.label for point in locations],
            durations_minutes=durations,
            distances_km=distances,
            source_ids=sorted(source_ids),
        )
        self.matrices[key] = matrix
        return matrix
