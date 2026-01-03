"""Construct the tool catalog from the backend_code-local environment file."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv
from pydantic import ValidationError

from .bookings import (
    FlightSearchTool,
    GroundTransportSearchTool,
    HotelSearchTool,
    TransportSearchTool,
)
from .bright_data import (
    BrightDataFlightProvider,
    BrightDataHotelProvider,
    BrightDataScraperClient,
    parse_hotel_output_fields,
    parse_template,
)
from .contracts import (
    HotelSearchInput,
    MapLookupInput,
    PlaceLookupInput,
    ToolResult,
    ToolStatus,
    TransportSearchInput,
    WeatherInput,
)
from .currency import CurrencyTool
from .ground_transit import GoogleTransitProvider
from .news import EventsSearchTool
from .places import PlacesProvider, PlacesTools
from .routes import RoutesTools
from .run_guard import ToolRunGuard
from .weather import WeatherTool


@dataclass
class PlanningToolCatalog:
    places: PlacesProvider
    routes: RoutesTools
    weather: WeatherTool
    flights: FlightSearchTool
    hotels: HotelSearchTool
    ground_transport: GroundTransportSearchTool
    currency: CurrencyTool
    events: EventsSearchTool
    bright_data: BrightDataScraperClient | None = None
    transit_provider: GoogleTransitProvider | None = None
    run_guard: ToolRunGuard = field(default_factory=ToolRunGuard)

    @property
    def transport(self) -> TransportSearchTool:
        return TransportSearchTool(self.flights, self.ground_transport)

    def agent_tools(self) -> dict[str, object]:
        """Five required tool categories from the architecture diagram.

        Currency and scheduling/cost calculations remain internal helpers;
        optional news/events is omitted until explicitly enabled.
        """
        async def places(query):
            try:
                validated = PlaceLookupInput.model_validate(query)
            except ValidationError:
                return ToolResult(status=ToolStatus.ERROR, provider="places",
                                  error_code="invalid_input",
                                  warnings=["Specify one valid Places action with its required fields."])
            return await self.run_guard.call("places", validated, self.places.lookup)

        async def maps(query):
            try:
                validated = MapLookupInput.model_validate(query)
            except ValidationError:
                return ToolResult(status=ToolStatus.ERROR, provider="maps",
                                  error_code="invalid_input",
                                  warnings=["Specify one valid Maps action with coordinates, mode, and a timezone-aware departure time if supplied."])
            return await self.run_guard.call("maps", validated, self.routes.lookup)

        async def weather(query):
            try:
                validated = WeatherInput.model_validate(query)
            except ValidationError:
                return ToolResult(status=ToolStatus.ERROR, provider="weather",
                                  error_code="invalid_input",
                                  warnings=["Weather input must contain valid coordinates and ordered dates spanning at most 31 days."])
            return await self.run_guard.call("weather", validated, self.weather.forecast)

        async def transport(query):
            try:
                validated = TransportSearchInput.model_validate(query)
            except ValidationError:
                return ToolResult(status=ToolStatus.ERROR, provider="transport",
                                  error_code="invalid_input",
                                  warnings=["Specify one valid flight or transit request with its required dates, travelers and mode fields."])
            return await self.run_guard.call("transport", validated, self.transport.search)

        async def accommodation(query):
            try:
                validated = HotelSearchInput.model_validate(query)
            except ValidationError:
                return ToolResult(status=ToolStatus.ERROR, provider="accommodation",
                                  error_code="invalid_input",
                                  warnings=["Specify a destination, ordered stay dates, valid guests, budget, currency, and continuation token."])
            return await self.run_guard.call("accommodation", validated, self.hotels.search)

        return {"places": places, "maps": maps, "weather": weather,
                "transport": transport, "accommodation": accommodation}

    def tool_usage(self) -> dict[str, dict[str, int]]:
        return self.run_guard.usage()

    def provider_usage(self) -> dict[str, int]:
        return {"bright_data_jobs_started": self.bright_data.jobs_started if self.bright_data else 0,
                "google_transit_requests": self.transit_provider.requests_made if self.transit_provider else 0}

    @classmethod
    def from_local_env(cls) -> "PlanningToolCatalog":
        from app.config import ENV_FILE
        load_dotenv(ENV_FILE, override=False)
        key = os.getenv("GOOGLE_MAPS_API_KEY")
        bright_token = os.getenv("BRIGHT_DATA_API_TOKEN")
        flight_id = os.getenv("BRIGHT_DATA_FLIGHTS_DATASET_ID") or "gd_mhng7wen1rw0a3gvpf"
        hotel_id = os.getenv("BRIGHT_DATA_HOTELS_DATASET_ID")
        hotel_discover_by = os.getenv("BRIGHT_DATA_HOTELS_DISCOVER_BY")
        hotel_booking_pricing = os.getenv("BRIGHT_DATA_HOTELS_FORMAT") == "booking_pricing"
        flight_template = parse_template(os.getenv("BRIGHT_DATA_FLIGHTS_INPUT_TEMPLATE")) or {
            "origin": "$origin", "destination": "$destination", "departure": "$departure_timestamp",
            "trip_type": "$trip_type", "adults": "$adults", "cabin": "$cabin",
            "currency": "$currency", "language": "$language", "country": "$country",
            "children": "$children", "infants_in_seat": "$infants_in_seat",
            "infants_on_lap": "$infants_on_lap",
        }
        try:
            hotel_template = parse_template(os.getenv("BRIGHT_DATA_HOTELS_INPUT_TEMPLATE"))
            hotel_fields = parse_hotel_output_fields(os.getenv("BRIGHT_DATA_HOTELS_OUTPUT_FIELDS"))
        except ValueError:
            hotel_template = hotel_fields = None  # Invalid hotel config never starts a paid job.
        scraper = BrightDataScraperClient(bright_token) if bright_token and (
            flight_id and flight_template or hotel_id and hotel_template and hotel_fields
        ) else None
        flight_provider = BrightDataFlightProvider(scraper, flight_id, flight_template) if (
            scraper and flight_id and flight_template
        ) else None
        hotel_provider = BrightDataHotelProvider(scraper, hotel_id, hotel_template, hotel_fields,
                                                 discover_by=hotel_discover_by,
                                                 booking_pricing=hotel_booking_pricing) if (
            scraper and hotel_id and hotel_template and hotel_fields
        ) else None
        transit_provider = GoogleTransitProvider(key)
        return cls(
            places=PlacesTools(key), routes=RoutesTools(key), weather=WeatherTool(),
            flights=FlightSearchTool(flight_provider), hotels=HotelSearchTool(hotel_provider),
            ground_transport=GroundTransportSearchTool(transit_provider), currency=CurrencyTool(),
            events=EventsSearchTool(), bright_data=scraper, transit_provider=transit_provider,
        )

    async def close(self) -> None:
        await self.run_guard.close()
        await asyncio.gather(
            self.places.close(), self.routes.close(), self.weather.close(), self.currency.close(),
            *([self.bright_data.close()] if self.bright_data else []),
            *([self.transit_provider.close()] if self.transit_provider else []),
        )
