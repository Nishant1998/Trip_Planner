"""Build the exact route pairs that the itinerary materializer can use."""

from __future__ import annotations

from app.models.draft import ItineraryDraft
from app.models.evidence import (
    EvidenceBundle,
    GeoPoint,
    IntercityOptionEvidence,
    PlaceEvidence,
)
from app.models.context import (
    PlanningContext,
    ResolvedDestination,
    TripSkeleton,
)
from app.models.route import RouteRequest
from app.domain.closing_journey import build_closing_legs


def build_route_requests(
    context: PlanningContext,
    destination: ResolvedDestination,
    skeleton: TripSkeleton,
    draft: ItineraryDraft,
    evidence: EvidenceBundle,
) -> list[RouteRequest]:
    """Mirror the materializer's base, stop, POI, and return movements."""
    builder = _RouteRequestBuilder(context, destination, evidence)
    places = evidence.place_map()
    draft_days = {day.day_number: day for day in draft.days}
    previous_base = context.origin

    for day in skeleton.days:
        current_stop = previous_base
        current_place: PlaceEvidence | None = None
        visited_stops = {previous_base.casefold()}
        day_places: list[PlaceEvidence] = []
        first_stop = day.stop_names[0] if day.stop_names else day.base_stop

        if current_stop.casefold() != first_stop.casefold():
            builder.add_long(current_stop, first_stop)
            current_stop = first_stop
            visited_stops.add(first_stop.casefold())

        draft_day = draft_days.get(day.day_number)
        for activity in draft_day.activities if draft_day else []:
            place = places.get(activity.evidence_place_id or "")
            if place is None:
                continue
            day_places.append(place)

            if place.stop_name.casefold() != current_stop.casefold():
                if current_place is not None:
                    builder.add_local(
                        current_place.name,
                        current_stop,
                        origin_location=current_place.location,
                    )
                    current_place = None
                builder.add_long(current_stop, place.stop_name)
                current_stop = place.stop_name
                visited_stops.add(current_stop.casefold())

            local_origin = current_place.name if current_place else current_stop
            if local_origin.casefold() != place.name.casefold():
                builder.add_local(
                    local_origin,
                    place.name,
                    origin_location=current_place.location if current_place else None,
                    destination_location=place.location,
                )
            current_place = place

        _add_routes_after_possible_activity_drops(builder, day_places, day.base_stop)

        for planned_stop in day.stop_names:
            if planned_stop.casefold() in visited_stops:
                continue
            builder.add_long(current_stop, planned_stop)
            current_stop = planned_stop
            current_place = None
            visited_stops.add(planned_stop.casefold())

        return_target = (
            context.end_location if day.day_number == len(skeleton.days) else None
        )
        for leg in build_closing_legs(
            current_stop=current_stop,
            current_place_name=current_place.name
            if current_place is not None
            else None,
            planned_stops=day.stop_names,
            visited_stops=visited_stops,
            base_stop=day.base_stop,
            return_target=return_target,
        ):
            if leg.kind == "local":
                builder.add_local(leg.origin, leg.destination)
            else:
                builder.add_long(leg.origin, leg.destination)

        previous_base = day.base_stop

    return builder.requests


def _add_routes_after_possible_activity_drops(
    builder: _RouteRequestBuilder,
    places: list[PlaceEvidence],
    base_stop: str,
) -> None:
    """Cover route pairs that become adjacent when an overfull activity is removed."""
    for index, place in enumerate(places):
        builder.add_local(
            place.stop_name,
            place.name,
            destination_location=place.location,
        )
        builder.add_local(
            place.name,
            place.stop_name,
            origin_location=place.location,
        )
        builder.add_local(
            place.name,
            base_stop,
            origin_location=place.location,
        )
        for earlier in places[:index]:
            if earlier.stop_name.casefold() == place.stop_name.casefold():
                builder.add_local(
                    earlier.name,
                    place.name,
                    origin_location=earlier.location,
                    destination_location=place.location,
                )


class _RouteRequestBuilder:
    """Keep mode selection and route de-duplication in one small helper."""

    def __init__(
        self,
        context: PlanningContext,
        destination: ResolvedDestination,
        evidence: EvidenceBundle,
    ) -> None:
        self.context = context
        self.countries = {
            stop.name.casefold(): stop.country_code
            for stop in destination.stops
            if stop.country_code
        }
        self._requests: dict[tuple[str, str], RouteRequest] = {}
        self.intercity_options = evidence.intercity_options

    @property
    def requests(self) -> list[RouteRequest]:
        return list(self._requests.values())

    def add_long(self, origin: str, destination: str) -> None:
        origin_country = self.countries.get(origin.casefold()) or _country_from_label(
            origin
        )
        destination_country = self.countries.get(
            destination.casefold()
        ) or _country_from_label(destination)
        option = _best_intercity_option(
            origin,
            destination,
            self.context.long_distance_modes,
            self.intercity_options,
        )
        preferred = (
            self.context.long_distance_modes[0]
            if self.context.long_distance_modes
            else "car"
        )
        mode = option.mode if option is not None else preferred
        if (
            origin_country
            and destination_country
            and origin_country != destination_country
        ):
            mode = option.mode if option is not None else "flight"
        self._add(origin, destination, mode)

    def add_local(
        self,
        origin: str,
        destination: str,
        *,
        origin_location: GeoPoint | None = None,
        destination_location: GeoPoint | None = None,
    ) -> None:
        mode = (
            self.context.local_transport_modes[0]
            if self.context.local_transport_modes
            else "car"
        )
        self._add(
            origin,
            destination,
            mode,
            origin_location=origin_location,
            destination_location=destination_location,
        )

    def _add(
        self,
        origin: str,
        destination: str,
        mode: str,
        *,
        origin_location: GeoPoint | None = None,
        destination_location: GeoPoint | None = None,
    ) -> None:
        if origin.casefold() == destination.casefold():
            return
        key = (origin.casefold(), destination.casefold())
        self._requests.setdefault(
            key,
            RouteRequest(
                origin_name=origin,
                destination_name=destination,
                mode=mode,
                origin_location=origin_location,
                destination_location=destination_location,
            ),
        )


def _country_from_label(label: str) -> str | None:
    normalized = label.casefold()
    if any(value in normalized for value in ("india", "mumbai", "delhi", "pune")):
        return "IN"
    if any(value in normalized for value in ("japan", "tokyo", "kyoto", "osaka")):
        return "JP"
    return None


def _best_intercity_option(
    origin: str,
    destination: str,
    allowed_modes: list[str],
    options: list[IntercityOptionEvidence],
) -> IntercityOptionEvidence | None:
    normalized_modes = [mode.casefold() for mode in allowed_modes]
    matching = [
        option
        for option in options
        if option.origin_name.casefold() == origin.casefold()
        and option.destination_name.casefold() == destination.casefold()
        and (not normalized_modes or option.mode.casefold() in normalized_modes)
    ]
    if not matching:
        return None
    return min(
        matching,
        key=lambda option: (
            (
                normalized_modes.index(option.mode.casefold())
                if option.mode.casefold() in normalized_modes
                else len(normalized_modes)
            ),
            option.maximum_duration_minutes,
        ),
    )
