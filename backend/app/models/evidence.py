"""Normalized evidence returned by external providers."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class FactStatus(StrEnum):
    VERIFIED = "verified"
    ESTIMATED = "estimated"
    SYNTHETIC = "synthetic"
    UNAVAILABLE = "unavailable"


class SourceRef(BaseModel):
    id: str
    provider: str
    kind: str
    title: str
    url: str | None = None
    provider_record_id: str | None = None
    retrieved_at: datetime | None = None


class GeoPoint(BaseModel):
    latitude: float
    longitude: float


class WeeklyOpeningPeriod(BaseModel):
    """One provider-backed weekly opening interval; Monday is day zero."""

    open_day: int = Field(ge=0, le=6)
    open_time: time
    close_day: int | None = Field(default=None, ge=0, le=6)
    close_time: time | None = None


class PlaceEvidence(BaseModel):
    """One provider-backed place as used in one planned stop.

    ``id`` belongs to this application and must be unique inside an
    ``EvidenceBundle``.  ``provider_place_id`` is the provider's original ID.
    A real place can appear in searches for two nearby stops, so the provider
    ID alone is not safe as our internal key.
    """

    id: str
    name: str
    stop_name: str
    categories: list[str] = Field(default_factory=list)
    address: str | None = None
    image_url: str | None = None
    location: GeoPoint | None = None
    provider_place_id: str | None = None
    google_maps_uri: str | None = None
    estimated_visit_minutes: int = Field(default=90, ge=15)
    opening_hours: str | None = None
    weekday_hours: list[str] = Field(default_factory=list)
    opening_periods: list[WeeklyOpeningPeriod] = Field(default_factory=list)
    status: FactStatus = FactStatus.VERIFIED
    source_ids: list[str] = Field(default_factory=list)
    is_primary_destination: bool = False


class RouteEvidence(BaseModel):
    id: str
    origin_name: str
    destination_name: str
    origin_location: GeoPoint | None = None
    destination_location: GeoPoint | None = None
    mode: str
    distance_km: float | None = None
    duration_minutes: int | None = None
    minimum_cost: Decimal | None = Field(default=None, ge=0)
    maximum_cost: Decimal | None = Field(default=None, ge=0)
    cost_currency_code: str | None = None
    cost_basis: str | None = None
    status: FactStatus
    source_ids: list[str] = Field(default_factory=list)


class WeatherEvidence(BaseModel):
    id: str
    stop_name: str
    date: date
    summary: str
    minimum_temperature_c: float | None = None
    maximum_temperature_c: float | None = None
    precipitation_probability: int | None = Field(default=None, ge=0, le=100)
    timezone: str | None = None
    status: FactStatus
    source_ids: list[str] = Field(default_factory=list)


class CurrencyRateEvidence(BaseModel):
    id: str
    base_currency: str
    quote_currency: str
    rate: Decimal
    rate_date: date
    status: FactStatus
    source_ids: list[str] = Field(default_factory=list)


class WebFactEvidence(BaseModel):
    """A current web-researched claim that still needs traveller confirmation."""

    id: str
    entity_name: str
    fact_type: str
    summary: str
    applicable_date: date | None = None
    status: FactStatus = FactStatus.ESTIMATED
    source_ids: list[str] = Field(default_factory=list)


class IntercityOptionEvidence(BaseModel):
    """A source-backed mode-level option, never a promised service or fare."""

    id: str
    origin_name: str
    destination_name: str
    mode: str
    minimum_duration_minutes: int = Field(ge=1)
    maximum_duration_minutes: int = Field(ge=1)
    minimum_cost: Decimal = Field(ge=0)
    maximum_cost: Decimal = Field(ge=0)
    currency_code: str
    basis: str
    status: FactStatus = FactStatus.ESTIMATED
    source_ids: list[str] = Field(default_factory=list)


class CostBaselineEvidence(BaseModel):
    """A destination-level planning range, not a quote or availability claim."""

    id: str
    stop_name: str
    category: str
    minimum: Decimal = Field(ge=0)
    maximum: Decimal = Field(ge=0)
    currency_code: str
    unit: str
    status: FactStatus = FactStatus.ESTIMATED
    source_ids: list[str] = Field(default_factory=list)


class VisitDurationEvidence(BaseModel):
    """A source-backed reasonable time range for one exact place."""

    id: str
    place_id: str
    minimum_minutes: int = Field(ge=15, le=720)
    recommended_minutes: int = Field(ge=15, le=720)
    maximum_minutes: int = Field(ge=15, le=720)
    access_minutes: int | None = Field(default=None, ge=0, le=180)
    waiting_minutes: int | None = Field(default=None, ge=0, le=360)
    rest_minutes: int | None = Field(default=None, ge=0, le=180)
    exit_minutes: int | None = Field(default=None, ge=0, le=120)
    physical_effort: Literal["low", "moderate", "high"] | None = None
    last_entry_minutes_before_close: int | None = Field(
        default=None,
        ge=0,
        le=240,
    )
    confidence: Literal["low", "medium", "high"] = "medium"
    basis: str
    status: FactStatus = FactStatus.ESTIMATED
    source_ids: list[str] = Field(default_factory=list)


class CurrentTravelResearch(BaseModel):
    places: list[PlaceEvidence] = Field(default_factory=list)
    web_facts: list[WebFactEvidence] = Field(default_factory=list)
    intercity_options: list[IntercityOptionEvidence] = Field(default_factory=list)
    cost_baselines: list[CostBaselineEvidence] = Field(default_factory=list)
    visit_durations: list[VisitDurationEvidence] = Field(default_factory=list)


class EvidenceBundle(BaseModel):
    places: list[PlaceEvidence] = Field(default_factory=list)
    routes: list[RouteEvidence] = Field(default_factory=list)
    weather: list[WeatherEvidence] = Field(default_factory=list)
    currency_rates: list[CurrencyRateEvidence] = Field(default_factory=list)
    web_facts: list[WebFactEvidence] = Field(default_factory=list)
    intercity_options: list[IntercityOptionEvidence] = Field(default_factory=list)
    cost_baselines: list[CostBaselineEvidence] = Field(default_factory=list)
    visit_durations: list[VisitDurationEvidence] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    provider_status: dict[str, str] = Field(default_factory=dict)

    def place_map(self) -> dict[str, PlaceEvidence]:
        """Return places by internal ID without silently losing duplicates."""

        places: dict[str, PlaceEvidence] = {}
        for place in self.places:
            if place.id in places:
                raise ValueError(
                    f"Duplicate internal place ID {place.id!r}; "
                    "place IDs must be unique for each stop."
                )
            places[place.id] = place
        return places

    def source_ids(self) -> set[str]:
        return {source.id for source in self.sources}
