"""Structured route requests and conservative travel windows."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.evidence import GeoPoint


class RouteRequest(BaseModel):
    origin_name: str
    destination_name: str
    mode: str
    origin_location: GeoPoint | None = None
    destination_location: GeoPoint | None = None


class RouteMatrixLocation(BaseModel):
    """One named point included in a provider travel-time matrix."""

    label: str
    location: GeoPoint | None = None


class RouteMatrix(BaseModel):
    """Pairwise road durations and distances in the supplied location order."""

    labels: list[str]
    durations_minutes: list[list[int | None]]
    distances_km: list[list[float | None]]
    source_ids: list[str] = Field(default_factory=list)


class TravelWindow(BaseModel):
    nominal_minutes: int = Field(ge=1)
    uncertainty_buffer_minutes: int = Field(ge=0)
    access_buffer_minutes: int = Field(ge=0)
    planned_minutes: int = Field(ge=1)
    basis: str
