"""Booking search boundaries. No provider or credentials are assumed."""

from __future__ import annotations

from typing import Protocol

from .contracts import (
    BookingOption, FlightSearchInput, GroundTransportInput, HotelSearchInput,
    ToolResult, ToolStatus, TransportOption, TransportSearchInput,
)
from .http import failure, unavailable


class FlightProvider(Protocol):
    async def search(self, query: FlightSearchInput) -> ToolResult[list[TransportOption]]: ...


class HotelProvider(Protocol):
    async def search(self, query: HotelSearchInput) -> ToolResult[list[BookingOption]]: ...


class GroundTransportProvider(Protocol):
    async def search(self, query: GroundTransportInput) -> ToolResult[list[TransportOption]]: ...


class FlightSearchTool:
    def __init__(self, provider: FlightProvider | None = None):
        self.provider = provider

    async def search(self, query: FlightSearchInput) -> ToolResult[list[TransportOption]]:
        if self.provider is None:
            return unavailable("flight_search", "No flight search API is configured.")
        try:
            return ToolResult[list[TransportOption]].model_validate(await self.provider.search(query))
        except Exception as exc:
            return failure("flight_search", exc)


class HotelSearchTool:
    def __init__(self, provider: HotelProvider | None = None):
        self.provider = provider

    async def search(self, query: HotelSearchInput) -> ToolResult[list[BookingOption]]:
        if query.check_out <= query.check_in:
            raise ValueError("check_out must be after check_in")
        if self.provider is None:
            return unavailable("hotel_search", "No hotel search API is configured.")
        try:
            return ToolResult[list[BookingOption]].model_validate(await self.provider.search(query))
        except Exception as exc:
            return failure("hotel_search", exc)


class GroundTransportSearchTool:
    def __init__(self, provider: GroundTransportProvider | None = None):
        self.provider = provider

    async def search(self, query: GroundTransportInput) -> ToolResult[list[TransportOption]]:
        if self.provider is None:
            return unavailable("ground_transport_search", "No train, bus, or car search API is configured.")
        try:
            return ToolResult[list[TransportOption]].model_validate(await self.provider.search(query))
        except Exception as exc:
            return failure("ground_transport_search", exc)


class TransportSearchTool:
    """One transport entrypoint for flight and ground-search providers."""

    def __init__(self, flights: FlightSearchTool, ground: GroundTransportSearchTool):
        self.flights = flights
        self.ground = ground

    async def search(self, query: TransportSearchInput):
        query = TransportSearchInput.model_validate(query)
        if query.mode == "flight":
            if query.trip_type == "Multi-city":
                options: list[TransportOption] = []
                warnings = ["Multi-city legs are separate one-way searches, not one ticket or a combined fare."]
                pending: dict[int, str] = {}
                statuses = []
                assert query.legs is not None
                base, remainder = divmod(10, len(query.legs))
                for index, leg in enumerate(query.legs):
                    if query.continuation_tokens and index not in query.continuation_tokens:
                        continue  # Caller retains previously completed legs; never restart them.
                    result = await self.flights.search(FlightSearchInput(
                        origin=leg.origin, destination=leg.destination,
                        departure_date=leg.departure_date, adults=query.passengers,
                        children=query.children, infants_in_seat=query.infants_in_seat,
                        infants_on_lap=query.infants_on_lap, cabin=query.cabin or "Economy",
                        currency=query.currency, language=query.language, country=query.country,
                        result_limit=base + (index < remainder),
                        continuation_token=query.continuation_tokens.get(index),
                    ))
                    statuses.append(result.status)
                    if result.continuation_token:
                        pending[index] = result.continuation_token
                    warnings.extend(f"Leg {index + 1}: {message}" for message in result.warnings)
                    for option in result.data or []:
                        options.append(option.model_copy(update={"leg_index": index}))
                status = (ToolStatus.PARTIAL if options or pending or ToolStatus.OK in statuses else
                          ToolStatus.UNAVAILABLE if all(s == ToolStatus.UNAVAILABLE for s in statuses) else ToolStatus.ERROR)
                return ToolResult(status=status, data=options, provider="bright_data_google_flights",
                                  warnings=warnings, continuation_tokens=pending)
            return await self.flights.search(FlightSearchInput(
                origin=query.origin, destination=query.destination,
                departure_date=query.departure_date, adults=query.passengers,
                children=query.children, infants_in_seat=query.infants_in_seat,
                infants_on_lap=query.infants_on_lap, cabin=query.cabin or "Economy",
                currency=query.currency, language=query.language, country=query.country,
                trip_type=query.trip_type, return_date=query.return_date,
                continuation_token=query.continuation_token,
            ))
        return await self.ground.search(GroundTransportInput(
            origin=query.origin, destination=query.destination,
            departure_time=query.departure_time, mode=query.mode,
            passengers=query.passengers,
        ))
