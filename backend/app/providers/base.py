"""Replaceable provider interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from app.providers.tool_loop import ToolCallingModel
from app.models.draft import DraftResult, ItineraryDraft
from app.models.evidence import (
    CurrencyRateEvidence,
    CurrentTravelResearch,
    GeoPoint,
    PlaceEvidence,
    RouteEvidence,
    WeatherEvidence,
)
from app.models.context import (
    PlanningContext,
    ResolvedDestination,
    ResolvedStop,
    TripSkeleton,
)
from app.models.route import RouteMatrix, RouteMatrixLocation
from app.models.plan import ValidationIssue


class DestinationResolver(Protocol):
    async def resolve(self, context: PlanningContext) -> ResolvedDestination: ...


class PlaceProvider(Protocol):
    async def search_places(
        self,
        stop: ResolvedStop,
        context: PlanningContext,
    ) -> list[PlaceEvidence]: ...


class RouteProvider(Protocol):
    async def get_route(
        self,
        origin_name: str,
        destination_name: str,
        mode: str,
        *,
        origin_location: GeoPoint | None = None,
        destination_location: GeoPoint | None = None,
    ) -> RouteEvidence: ...

    async def get_route_matrix(
        self,
        locations: list[RouteMatrixLocation],
        mode: str,
    ) -> RouteMatrix: ...


class WeatherProvider(Protocol):
    async def get_weather(
        self,
        stop: ResolvedStop,
        start_date: date,
        end_date: date,
    ) -> list[WeatherEvidence]: ...


class CurrencyProvider(Protocol):
    async def get_rate(
        self,
        base_currency: str,
        quote_currency: str,
    ) -> CurrencyRateEvidence: ...


class CurrentFactsProvider(Protocol):
    async def research(
        self,
        context: PlanningContext,
        destination: ResolvedDestination,
        places: list[PlaceEvidence],
    ) -> CurrentTravelResearch: ...


class ItineraryProvider(Protocol):
    async def create_draft(
        self,
        context: PlanningContext,
        destination: ResolvedDestination,
        skeleton: TripSkeleton,
        places: list[PlaceEvidence],
    ) -> DraftResult: ...

    async def repair_draft(
        self,
        context: PlanningContext,
        destination: ResolvedDestination,
        skeleton: TripSkeleton,
        places: list[PlaceEvidence],
        draft: ItineraryDraft,
        issues: list[ValidationIssue],
    ) -> DraftResult: ...


@dataclass(frozen=True, slots=True)
class ProviderBundle:
    """All replaceable I/O boundaries required by one planner run."""

    destination: DestinationResolver
    places: PlaceProvider
    routes: RouteProvider
    weather: WeatherProvider
    currency: CurrencyProvider
    itinerary: ItineraryProvider
    agent_model: ToolCallingModel | None = None
    current_facts: CurrentFactsProvider | None = None
