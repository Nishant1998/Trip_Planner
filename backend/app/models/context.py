"""Normalized internal context used by the planning workflow."""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DestinationSpec(BaseModel):
    """Confirmed destination supplied by Riya."""

    model_config = ConfigDict(extra="allow")

    type: str
    items: list[str] = Field(default_factory=list)
    subtype: str | None = None


class ResolvedStop(BaseModel):
    """Planner-ready destination stop."""

    name: str
    country_code: str | None = None
    currency_code: str | None = None
    suggested_days: int = Field(default=1, ge=1)
    reason: str | None = None
    geocode_query: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class ResolvedDestination(BaseModel):
    """Structured interpretation produced by the resolver."""

    interpretation: str
    stops: list[ResolvedStop]
    assumptions: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)


class PlanningContext(BaseModel):
    """Flattened, typed view of one confirmed TripMemory object."""

    trip_id: str | None = None
    trip_name: str | None = None
    trip_description: str | None = None
    currency_code: str
    timezone: str | None = None
    trip_scope: str | None = None

    origin: str
    destination: DestinationSpec
    start_date: date
    end_date: date
    duration_days: int
    num_adults: int
    num_children: int = 0
    children_ages: list[int] = Field(default_factory=list)
    num_seniors: int = 0
    trip_budget: Decimal

    day_start_time: time = time(9, 0)
    day_end_time: time = time(20, 0)
    travel_style: str = "moderate"
    end_location: str | None = None

    requirements: dict[str, Any] = Field(default_factory=dict)
    field_sources: dict[str, str] = Field(default_factory=dict)
    trip_context_notes: list[str] = Field(default_factory=list)

    destination_verified: bool = False
    resolved_city_names: list[str] = Field(default_factory=list)
    destination_intelligence: dict[str, Any] | None = None
    data_confidence: str | None = None

    warnings: list[str] = Field(default_factory=list)

    @property
    def traveller_count(self) -> int:
        return self.num_adults + self.num_children + self.num_seniors

    @property
    def activity_interests(self) -> list[str]:
        return self._list_requirement("activity_interests")

    @property
    def must_see(self) -> list[str]:
        return self._list_requirement("must_see")

    @property
    def avoid(self) -> list[str]:
        return self._list_requirement("avoid")

    @property
    def dietary_restrictions(self) -> list[str]:
        return self._list_requirement("dietary_restrictions")

    @property
    def cuisine_preferences(self) -> list[str]:
        return self._list_requirement("cuisine_prefs")

    @property
    def accessibility_needs(self) -> list[str]:
        return self._list_requirement("accessibility_needs")

    @property
    def long_distance_modes(self) -> list[str]:
        return self._list_requirement("long_distance_modes")

    @property
    def local_transport_modes(self) -> list[str]:
        return self._list_requirement("local_transport_modes")

    def planning_input_groups(self) -> dict[str, object]:
        """Separate confirmed non-negotiables from preferences for planning agents.

        trip-detail collection has already obtained user approval.  These groups therefore do
        not invite the planner to reinterpret the trip; they make explicit
        which approved values it must preserve and which values can guide a
        choice between otherwise feasible itinerary options.
        """

        hard_fields = {
            "origin",
            "destination",
            "start_date",
            "end_date",
            "trip_duration_days",
            "num_adults",
            "num_children",
            "children_ages",
            "num_seniors",
            "trip_budget",
            "day_start_time",
            "day_end_time",
            "end_location",
            "long_distance_modes",
            "local_transport_modes",
            "must_see",
            "avoid",
            "dietary_restrictions",
            "accessibility_needs",
        }
        present = {
            name: value
            for name, value in self.requirements.items()
            if value is not None
        }
        return {
            "hard_requirements": {
                name: value for name, value in present.items() if name in hard_fields
            },
            "soft_preferences": {
                name: value
                for name, value in present.items()
                if name not in hard_fields
            },
            "planning_context_notes": list(self.trip_context_notes),
        }

    def _list_requirement(self, name: str) -> list[str]:
        value = self.requirements.get(name)
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value]


class DaySkeleton(BaseModel):
    """Coarse allocation made before activity selection."""

    day_number: int
    date: date
    base_stop: str
    stop_names: list[str] = Field(default_factory=list)
    is_transfer_day: bool = False
    is_light_day: bool = False
    is_arrival_day: bool = False
    is_departure_day: bool = False
    max_major_activities: int
    day_start_time: time
    day_end_time: time


class TripSkeleton(BaseModel):
    """Coarse-to-fine day and stop allocation."""

    days: list[DaySkeleton]
    contingency_amount: Decimal
    spendable_budget: Decimal
    assumptions: list[str] = Field(default_factory=list)
