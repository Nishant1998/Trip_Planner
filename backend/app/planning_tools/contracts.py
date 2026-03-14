"""Shared input and output contracts for external and deterministic tools."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from datetime import date as Date
from enum import StrEnum
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field, field_validator, model_validator


class ToolStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


T = TypeVar("T")
BusinessStatus = Literal["BUSINESS_STATUS_UNSPECIFIED", "OPERATIONAL",
                         "CLOSED_TEMPORARILY", "CLOSED_PERMANENTLY", "FUTURE_OPENING"]


class ToolResult(BaseModel, Generic[T]):
    status: ToolStatus
    data: T | None = None
    provider: str
    retrieved_at: datetime | None = None
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None
    continuation_token: str | None = None
    continuation_tokens: dict[int, str] = Field(default_factory=dict)


class Coordinates(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class Place(BaseModel):
    place_id: str
    name: str
    location: Coordinates | None = None
    address: str | None = None
    types: list[str] = Field(default_factory=list)
    map_url: str | None = None
    opening_hours: dict | None = None
    business_status: BusinessStatus | None = None
    photo_names: list[str] = Field(default_factory=list)
    photos: list["PlacePhotoReference"] = Field(default_factory=list)


class PlacePhotoReference(BaseModel):
    name: str
    attribution: list[dict] = Field(default_factory=list)


class PlaceSearchInput(BaseModel):
    query: str = Field(min_length=1)
    max_results: int = Field(default=10, ge=1, le=10)
    center: Coordinates | None = None
    radius_m: float | None = Field(default=None, gt=0, le=50000)


class NearbySearchInput(BaseModel):
    center: Coordinates
    radius_m: float = Field(gt=0, le=50000)
    included_types: list[str] = Field(min_length=1)
    max_results: int = Field(default=10, ge=1, le=10)


class PlaceLookupInput(BaseModel):
    """One agent-facing Places tool; one selected provider operation per call."""

    action: Literal["text", "nearby", "details", "photo"] | None = None
    query: str | None = None
    center: Coordinates | None = None
    radius_m: float | None = Field(default=None, gt=0, le=50000)
    included_types: list[str] = Field(default_factory=list)
    max_results: int = Field(default=10, ge=1, le=10)
    place_id: str | None = None
    photo_name: str | None = Field(default=None, pattern=r"^places/[^/]+/photos/[^/]+$")
    max_width_px: int = Field(default=800, ge=1, le=4800)

    @model_validator(mode="after")
    def required_fields_for_action(self) -> "PlaceLookupInput":
        if self.action is None:
            if self.query and not (self.place_id or self.photo_name or self.included_types):
                self.action = "text"
            elif self.center and self.radius_m and self.included_types and not (self.query or self.place_id or self.photo_name):
                self.action = "nearby"
            else:
                raise ValueError("specify one Places action or an unambiguous search input")
        if self.action == "text":
            if not self.query or self.place_id or self.photo_name or self.included_types:
                raise ValueError("text lookup requires query and no unrelated fields")
            if (self.center is None) != (self.radius_m is None):
                raise ValueError("text location bias requires both center and radius_m")
        elif self.action == "nearby":
            if not (self.center and self.radius_m and self.included_types) or self.query or self.place_id or self.photo_name:
                raise ValueError("nearby lookup requires center, radius_m and types only")
        elif self.action == "details":
            if not self.place_id or self.query or self.center or self.radius_m or self.included_types or self.photo_name:
                raise ValueError("details lookup requires only place_id")
        elif self.action == "photo":
            if not self.photo_name or self.query or self.center or self.radius_m or self.included_types or self.place_id:
                raise ValueError("photo lookup requires only photo_name and optional width")
        return self


class Photo(BaseModel):
    uri: str | None = None
    attribution: list[dict] = Field(default_factory=list)


class WeatherDay(BaseModel):
    date: date
    weather_code: int | None = None
    min_c: float | None = None
    max_c: float | None = None
    precipitation_probability: int | None = Field(default=None, ge=0, le=100)


class HistoricalWeatherSummary(BaseModel):
    source: Literal["historical_estimate"] = "historical_estimate"
    start_date: date
    end_date: date
    typical_min_c: float
    typical_max_c: float
    wet_day_frequency_pct: float = Field(ge=0, le=100)
    sample_years: list[int] = Field(min_length=5)
    sample_days: int = Field(gt=0)


class WeatherReport(BaseModel):
    forecast_days: list[WeatherDay] = Field(default_factory=list)
    historical_summary: HistoricalWeatherSummary | None = None
    unavailable_dates: list[date] = Field(default_factory=list)


class WeatherInput(BaseModel):
    location: Coordinates
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def valid_range(self) -> "WeatherInput":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        if (self.end_date - self.start_date).days >= 31:
            raise ValueError("weather lookup is limited to 31 days per call")
        return self


class RoutePoint(BaseModel):
    id: str = Field(min_length=1)
    location: Coordinates


class MatrixInput(BaseModel):
    origins: list[RoutePoint] = Field(min_length=1, max_length=25)
    destinations: list[RoutePoint] = Field(min_length=1, max_length=25)
    mode: Literal["DRIVE", "WALK", "BICYCLE", "TRANSIT"] = "DRIVE"
    departure_time: datetime | None = None


class RouteInput(BaseModel):
    origin: RoutePoint
    destination: RoutePoint
    mode: Literal["DRIVE", "WALK", "BICYCLE", "TRANSIT"] = "DRIVE"
    departure_time: datetime | None = None


class MapLookupInput(BaseModel):
    """Select exactly one Routes API operation."""

    action: Literal["matrix", "directions"]
    origins: list[RoutePoint] | None = None
    destinations: list[RoutePoint] | None = None
    origin: RoutePoint | None = None
    destination: RoutePoint | None = None
    mode: Literal["DRIVE", "WALK", "BICYCLE", "TRANSIT"] = "DRIVE"
    departure_time: datetime | None = None

    @field_validator("departure_time")
    @classmethod
    def timezone_aware_departure(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("departure_time must include a timezone offset")
        return value

    @model_validator(mode="after")
    def required_fields_for_action(self) -> "MapLookupInput":
        if self.action == "matrix":
            if not self.origins or not self.destinations or self.origin or self.destination:
                raise ValueError("matrix requires origins and destinations only")
            if len(self.origins) * len(self.destinations) > 100:
                raise ValueError("agent matrix lookup is limited to 100 directed pairs")
            MatrixInput(origins=self.origins, destinations=self.destinations,
                        mode=self.mode, departure_time=self.departure_time)
        elif not (self.origin and self.destination) or self.origins or self.destinations:
            raise ValueError("directions requires origin and destination only")
        return self


class RouteLeg(BaseModel):
    origin_id: str
    destination_id: str
    mode: str
    distance_m: int | None = None
    duration_s: int | None = None
    polyline: str | None = None
    status: ToolStatus = ToolStatus.OK
    warning: str | None = None


class BookingOption(BaseModel):
    id: str
    name: str
    price: float | None = Field(default=None, ge=0)
    price_basis: Literal["total", "per_night", "unknown"] = "unknown"
    currency: str | None = None
    indicative_price: float | None = Field(default=None, ge=0)
    indicative_currency: str | None = None
    indicative_price_basis: Literal["total", "per_night", "unknown"] = "unknown"
    indicative_price_scope: Literal["adult_only"] | None = None
    location: Coordinates | None = None
    address: str | None = None
    departure: datetime | None = None
    arrival: datetime | None = None
    booking_url: str | None = None
    availability: Literal["available", "unknown"] = "unknown"


class FlightSearchInput(BaseModel):
    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    departure_date: date
    adults: int = Field(default=1, ge=1)
    children: int = Field(default=0, ge=0)
    infants_in_seat: int = Field(default=0, ge=0)
    infants_on_lap: int = Field(default=0, ge=0)
    cabin: Literal["Economy", "Premium economy", "Business", "First"] = "Economy"
    currency: str = Field(default="INR", pattern=r"^[A-Za-z]{3}$")
    language: str = Field(default="ENG", pattern=r"^[A-Za-z]{3}$")
    country: str = Field(default="IN", pattern=r"^[A-Za-z]{2}$")
    trip_type: Literal["One way", "Round trip"] = "One way"
    return_date: date | None = None
    result_limit: int = Field(default=10, ge=1, le=10)
    continuation_token: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")

    @model_validator(mode="after")
    def valid_flight(self) -> "FlightSearchInput":
        if not self.origin.strip() or not self.destination.strip() or self.origin.strip().casefold() == self.destination.strip().casefold():
            raise ValueError("flight endpoints must be different nonblank places")
        if self.departure_date < datetime.now(UTC).date():
            raise ValueError("flight departure date must not be in the past")
        if self.trip_type == "Round trip" and (self.return_date is None or self.return_date <= self.departure_date):
            raise ValueError("round trip requires a later return_date")
        if self.trip_type == "One way" and self.return_date is not None:
            raise ValueError("one-way flight must not have return_date")
        if self.infants_on_lap > self.adults:
            raise ValueError("lap infants cannot exceed adults")
        return self


class HotelSearchInput(BaseModel):
    destination: str = Field(min_length=1)
    check_in: date
    check_out: date
    adults: int = Field(default=1, ge=1)
    children: int = Field(default=0, ge=0)
    rooms: int = Field(default=1, ge=1)
    country: str | None = Field(default=None, pattern=r"^[A-Za-z]{2}$")
    budget_per_night: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    continuation_token: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("destination")
    @classmethod
    def nonblank_destination(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("destination must not be blank")
        return value

    @field_validator("currency")
    @classmethod
    def uppercase_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else None

    @field_validator("country")
    @classmethod
    def uppercase_country(cls, value: str | None) -> str | None:
        return value.upper() if value else None

    @model_validator(mode="after")
    def ordered_stay_dates(self) -> "HotelSearchInput":
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")
        return self


class GroundTransportInput(BaseModel):
    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    departure_time: datetime
    mode: Literal["train", "bus"]
    passengers: int = Field(default=1, ge=1)


class TransportSegment(BaseModel):
    mode: str
    origin: str | None = None
    destination: str | None = None
    departure: datetime | None = None
    arrival: datetime | None = None
    departure_local: str | None = None
    arrival_local: str | None = None
    carrier: str | None = None
    vehicle: str | None = None


class TransportOption(BaseModel):
    id: str
    requested_mode: Literal["flight", "train", "bus", "car"]
    leg_index: int = 0
    origin: str | None = None
    destination: str | None = None
    segments: list[TransportSegment] = Field(default_factory=list)
    departure: datetime | None = None
    arrival: datetime | None = None
    departure_local: str | None = None
    arrival_local: str | None = None
    departure_date: date | None = None
    duration_s: int | None = Field(default=None, ge=0)
    stops: int | None = Field(default=None, ge=0)
    fare_amount: float | None = Field(default=None, ge=0)
    fare_currency: str | None = None
    fare_classification: Literal["quoted", "estimated", "unknown"] = "unknown"
    fare_scope: Literal["passenger_group", "per_adult", "unknown"] = "unknown"
    source_url: str | None = None
    warnings: list[str] = Field(default_factory=list)


class TransportSearchInput(BaseModel):
    """One explicit transport mode; car routes belong to Maps, not ticket search."""

    mode: Literal["flight", "train", "bus"]
    origin: str | None = None
    destination: str | None = None
    departure_date: date | None = None
    departure_time: datetime | None = None
    passengers: int = Field(default=1, ge=1)
    children: int = Field(default=0, ge=0)
    infants_in_seat: int = Field(default=0, ge=0)
    infants_on_lap: int = Field(default=0, ge=0)
    cabin: Literal["Economy", "Premium economy", "Business", "First"] | None = None
    currency: str = Field(default="INR", pattern=r"^[A-Za-z]{3}$")
    language: str = Field(default="ENG", pattern=r"^[A-Za-z]{3}$")
    country: str = Field(default="IN", pattern=r"^[A-Za-z]{2}$")
    trip_type: Literal["One way", "Round trip", "Multi-city"] = "One way"
    return_date: date | None = None
    legs: list["FlightLegInput"] | None = Field(default=None, min_length=2, max_length=4)
    continuation_token: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")
    continuation_tokens: dict[int, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def mode_fields(self) -> "TransportSearchInput":
        if self.infants_on_lap > self.passengers:
            raise ValueError("lap infants cannot exceed adults")
        if self.origin is not None and (not self.origin.strip() or self.destination is None or not self.destination.strip() or self.origin.strip().casefold() == self.destination.strip().casefold()):
            raise ValueError("endpoints must be different nonblank places")
        if self.mode == "flight":
            if self.departure_time is not None:
                raise ValueError("departure_time applies only to ground transit")
            if self.trip_type == "Multi-city":
                if not self.legs or self.origin or self.destination or self.departure_date or self.return_date or self.continuation_token:
                    raise ValueError("multi-city requires only ordered legs")
                if any(index not in range(len(self.legs)) for index in self.continuation_tokens):
                    raise ValueError("invalid multi-city continuation index")
                if any(b.departure_date < a.departure_date for a, b in zip(self.legs, self.legs[1:])):
                    raise ValueError("multi-city leg dates must be ordered")
                if any(leg.departure_date < datetime.now(UTC).date() or not leg.origin.strip() or not leg.destination.strip() or leg.origin.strip().casefold() == leg.destination.strip().casefold() for leg in self.legs):
                    raise ValueError("multi-city legs need future dates and distinct endpoints")
            else:
                if not (self.origin and self.destination and self.departure_date) or self.legs or self.continuation_tokens:
                    raise ValueError("flight requires origin, destination and departure_date")
                FlightSearchInput(origin=self.origin, destination=self.destination, departure_date=self.departure_date,
                                  adults=self.passengers, children=self.children, infants_in_seat=self.infants_in_seat,
                                  infants_on_lap=self.infants_on_lap, trip_type=self.trip_type, return_date=self.return_date)
        else:
            if not (self.origin and self.destination and self.departure_time):
                raise ValueError("ground transit requires origin, destination and timezone-aware departure_time")
            if self.departure_time.tzinfo is None or self.departure_time.utcoffset() is None:
                raise ValueError("departure_time must include a timezone offset")
            now = datetime.now(UTC)
            if not now <= self.departure_time.astimezone(UTC) <= now + timedelta(days=100):
                raise ValueError("transit departure_time must be within the next 100 days")
            if self.departure_date or self.cabin or self.legs or self.continuation_token or self.continuation_tokens or self.return_date or self.trip_type != "One way" or self.children or self.infants_in_seat or self.infants_on_lap:
                raise ValueError("flight fields are not valid for ground transit")
        return self


class FlightLegInput(BaseModel):
    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    departure_date: date


class CurrencyInput(BaseModel):
    base: str = Field(pattern=r"^[A-Za-z]{3}$")
    quote: str = Field(pattern=r"^[A-Za-z]{3}$")
    amount: float = Field(ge=0)


class CurrencyQuote(BaseModel):
    base: str
    quote: str
    rate: float = Field(gt=0)
    amount: float = Field(ge=0)
    converted_amount: float = Field(ge=0)
    date: Date | None = None
