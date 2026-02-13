"""Bounded, route-aware schedule construction for V3."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from math import asin, ceil, cos, radians, sin, sqrt
import re
from uuid import uuid4

from app.models.candidate import (
    CandidateDay,
    CandidateFailure,
    CandidateSearchResult,
    FeasibleCandidate,
    ScheduledBreak,
    ScheduledItem,
    ScheduledTravel,
    ScheduledVisit,
)
from app.models.constraints import (
    AppliedRelaxation,
    ConstraintBundle,
    RelaxationAttempt,
    RelaxationStep,
)
from app.models.duration import VisitDuration
from app.models.evidence import EvidenceBundle, FactStatus, PlaceEvidence, RouteEvidence
from app.models.context import (
    PlanningContext,
    ResolvedDestination,
    ResolvedStop,
)
from app.models.preferences import PlannerIntent
from app.domain.duration import estimate_visit_duration
from app.domain.opening_hours import next_valid_visit_start
from app.domain.route_lookup import CachedRouteLookup, RoutePoint
from app.domain.route_optimizer import optimized_place_order
from app.domain.route_policy import SAME_LOCATION_DISTANCE_KM
from app.domain.stop_ordering import optimized_stop_order_variants
from app.domain.travel_buffer import calculate_travel_window

_LUNCH_EARLIEST = time(11, 30)
_LUNCH_LATEST_START = time(14, 0)
_LUNCH_DURATION_MINUTES = 60


@dataclass(frozen=True, slots=True)
class _SearchSettings:
    use_minimum_visit_duration: bool = False
    day_end_extension_minutes: int = 0
    long_mode_index: int = 0
    # When True the engine may return a partial plan that skips stops that
    # simply do not fit in the confirmed trip window, disclosing each omission
    # as a ``OMIT_UNFITTABLE_STOP`` relaxation. This is the last rung of the
    # ladder and is only tried after every earlier variant has failed.
    allow_partial_coverage: bool = False
    relaxations: tuple[AppliedRelaxation, ...] = ()


@dataclass(slots=True)
class _DayState:
    day_number: int
    date: date
    cursor: datetime
    end: datetime
    start_location: RoutePoint
    current: RoutePoint
    base: RoutePoint
    items: list[ScheduledItem] = field(default_factory=list)
    meal_added: bool = False
    rest_added: bool = False
    activity_count: int = 0

    def clone(self) -> _DayState:
        return _DayState(
            day_number=self.day_number,
            date=self.date,
            cursor=self.cursor,
            end=self.end,
            start_location=self.start_location,
            current=self.current,
            base=self.base,
            items=list(self.items),
            meal_added=self.meal_added,
            rest_added=self.rest_added,
            activity_count=self.activity_count,
        )


class FeasibilityEngine:
    """Generate a valid candidate or a precise bounded failure."""

    def __init__(self, routes: CachedRouteLookup) -> None:
        self.routes = routes
        self._last_stop_rejection: str | None = None

    async def search(
        self,
        *,
        context: PlanningContext,
        destination: ResolvedDestination,
        evidence: EvidenceBundle,
        constraints: ConstraintBundle,
        intent: PlannerIntent,
    ) -> CandidateSearchResult:
        places, score_map, input_failure = _validated_places(evidence, intent)
        if input_failure is not None:
            return CandidateSearchResult(failure=input_failure)

        settings = _search_settings(constraints, intent)
        routing_modes = _preferred_modes(
            intent.preferred_long_distance_modes or context.long_distance_modes,
            constraints.limits.allowed_long_distance_modes,
        )
        try:
            stop_orders = await optimized_stop_order_variants(
                context,
                destination.stops,
                self.routes,
                routing_modes[0],
            )
        except Exception:
            # Candidate route calls remain authoritative if the matrix service is down.
            stop_orders = [list(destination.stops)]
        attempts: list[RelaxationAttempt] = []
        latest_failure = CandidateFailure(
            issue_codes=["NO_FEASIBLE_CANDIDATE"],
            message="No route-aware schedule fitted the confirmed trip window.",
        )
        for option in settings:
            for stop_order in stop_orders:
                candidate, failure = await self._build(
                    context=context,
                    destination=destination,
                    evidence=evidence,
                    constraints=constraints,
                    intent=intent,
                    places=places,
                    score_map=score_map,
                    stop_order=stop_order,
                    settings=option,
                )
                attempts.append(
                    RelaxationAttempt(
                        step=(
                            option.relaxations[-1].step if option.relaxations else None
                        ),
                        feasible=candidate is not None,
                        issue_codes=failure.issue_codes if failure else [],
                        applied=list(option.relaxations),
                    )
                )
                if candidate is not None:
                    return CandidateSearchResult(candidate=candidate, attempts=attempts)
                if failure is not None:
                    latest_failure = failure
        return CandidateSearchResult(failure=latest_failure, attempts=attempts)

    async def _build(
        self,
        *,
        context: PlanningContext,
        destination: ResolvedDestination,
        evidence: EvidenceBundle,
        constraints: ConstraintBundle,
        intent: PlannerIntent,
        places: list[PlaceEvidence],
        score_map: dict[str, int],
        stop_order: list[ResolvedStop],
        settings: _SearchSettings,
    ) -> tuple[FeasibleCandidate | None, CandidateFailure | None]:
        self._last_stop_rejection = None
        required, optional, failure = _place_queues(
            places,
            stop_order,
            constraints,
            score_map,
        )
        if failure is not None:
            return None, failure

        long_modes = _preferred_modes(
            intent.preferred_long_distance_modes or context.long_distance_modes,
            constraints.limits.allowed_long_distance_modes,
        )
        local_modes = _preferred_modes(
            intent.preferred_local_modes or context.local_transport_modes,
            constraints.limits.allowed_local_modes,
        )
        long_mode = long_modes[min(settings.long_mode_index, len(long_modes) - 1)]
        local_mode = local_modes[0]
        remaining_stops = list(stop_order)
        visited_ids: list[str] = []
        covered_stops: list[str] = []
        days: list[CandidateDay] = []

        origin_route = next(
            (
                r
                for r in evidence.routes
                if r.origin_name.casefold() == context.origin.casefold()
                and r.origin_location is not None
            ),
            None,
        )
        origin_location = origin_route.origin_location if origin_route else None
        current_base = RoutePoint(context.origin, origin_location)

        # In the required-stops loop we track stops that could not be fit.
        # When ``allow_partial_coverage`` is true (last-resort settings level)
        # the engine skips over an infeasible stop instead of aborting the
        # whole plan. Every skip becomes an explicit disclosed relaxation.
        omitted_stops: list[ResolvedStop] = []

        for day_index in range(context.duration_days):
            day_date = context.start_date + timedelta(days=day_index)
            day_start = datetime.combine(day_date, constraints.limits.day_start_time)
            day_end = datetime.combine(
                day_date, constraints.limits.day_end_time
            ) + timedelta(minutes=settings.day_end_extension_minutes)
            state = _DayState(
                day_number=day_index + 1,
                date=day_date,
                cursor=day_start,
                end=day_end,
                start_location=current_base,
                current=current_base,
                base=current_base,
            )
            will_leave_overnight_base = bool(
                day_index > 0
                and (
                    any(
                        stop.name.casefold() != current_base.label.casefold()
                        for stop in remaining_stops
                    )
                    or (
                        day_index == context.duration_days - 1
                        and context.end_location is not None
                        and context.end_location.casefold()
                        != current_base.label.casefold()
                    )
                )
            )
            if will_leave_overnight_base and not _add_hotel_departure(state):
                return None, CandidateFailure(
                    issue_codes=["HOTEL_DEPARTURE_HANDLING_DOES_NOT_FIT"],
                    message="Hotel checkout and luggage handling do not fit in the day window.",
                    affected_ids=[current_base.label],
                )

            while remaining_stops:
                stop = remaining_stops[0]
                required_for_stop = required[stop.name.casefold()]
                # Do not consume every attraction for one broad stop on the
                # first day.  Reserve enough verified places for the later
                # usable days so a six-day Goa trip does not become five
                # busy/empty days merely because all POIs share one stop.
                stop_places = required_for_stop[
                    : _required_places_today(
                        len(required_for_stop),
                        remaining_days=context.duration_days - day_index,
                        reserve_return_day=bool(context.end_location),
                    )
                ]
                trial = await self._try_stop(
                    state,
                    stop,
                    stop_places,
                    context,
                    evidence,
                    long_mode,
                    local_mode,
                    settings.use_minimum_visit_duration,
                )
                if trial is None:
                    # Do NOT omit here. Move to the next day and try the same
                    # stop again from the current base. A stop is only truly
                    # unfittable (and eligible for omission) after the full
                    # day loop below has been exhausted.
                    break
                state = trial
                current_base = _stop_point(stop)
                visited_now = {place.id for place in stop_places}
                required[stop.name.casefold()] = [
                    place for place in required_for_stop if place.id not in visited_now
                ]
                visited_ids.extend(place.id for place in stop_places)
                if required[stop.name.casefold()]:
                    # This stop still has reserved POIs for later days.  Do
                    # not loop immediately and pack them into the same day.
                    break
                remaining_stops.pop(0)
                covered_stops.append(stop.name)

            # Preserve the required-only state. Optional visits must never crowd out
            # hotel handling or the final return journey.
            pre_optional_state = state.clone()
            # Fill only from the current base after required stops have been protected.
            base_key = state.base.label.casefold()
            for place in list(optional.get(base_key, [])):
                if state.activity_count >= _daily_activity_capacity(context):
                    break
                trial = await self._try_optional_place(
                    state,
                    place,
                    context,
                    evidence,
                    local_mode,
                    settings.use_minimum_visit_duration,
                )
                if trial is None:
                    continue
                state = trial
                optional[base_key].remove(place)
                visited_ids.append(place.id)

            # A long transfer may use a day even when the destination visit does not fit.
            if (
                not state.items
                and remaining_stops
                and day_index < context.duration_days - 1
            ):
                transferred = await self._try_transfer_day(
                    state,
                    remaining_stops[0],
                    long_mode,
                    context,
                    evidence,
                )
                if transferred is not None:
                    state = transferred
                    current_base = state.base

            is_last_day = day_index == context.duration_days - 1
            needs_hotel_arrival = bool(
                not is_last_day
                and state.base.label.casefold() != state.start_location.label.casefold()
            )
            if needs_hotel_arrival and not _add_hotel_arrival(state):
                if (
                    state.base.label.casefold()
                    == pre_optional_state.base.label.casefold()
                ):
                    state = pre_optional_state
                    state.end = day_end
                    arrival_added = _add_hotel_arrival(state)
                else:
                    arrival_added = False
                if not arrival_added and not _is_transfer_only_day(state):
                    return None, CandidateFailure(
                        issue_codes=["HOTEL_ARRIVAL_HANDLING_DOES_NOT_FIT"],
                        message="Hotel check-in, luggage handling, and settling time do not fit.",
                        affected_ids=[state.base.label],
                    )
            # On the last day, attempt the final return journey to the
            # traveller's end location. In aggressive (partial-coverage) mode
            # we still make the attempt even when some destination stops
            # remain, so the return can be scheduled instead of silently lost.
            attempt_return = (
                is_last_day
                and context.end_location is not None
                and (not remaining_stops or settings.allow_partial_coverage)
            )
            if attempt_return:
                assert context.end_location is not None
                end_route = next(
                    (
                        r
                        for r in evidence.routes
                        if r.destination_name.casefold()
                        == context.end_location.casefold()
                        and r.destination_location is not None
                    ),
                    None,
                )
                end_loc = (
                    end_route.destination_location if end_route else origin_location
                )
                end_point = RoutePoint(context.end_location, end_loc)
                returned = await self._append_route(
                    state,
                    end_point,
                    long_mode,
                    context,
                    evidence,
                )
                if returned is None:
                    # Drop only optional sightseeing before declaring the required
                    # homeward journey impossible.
                    returned = await self._append_route(
                        pre_optional_state.clone(),
                        end_point,
                        long_mode,
                        context,
                        evidence,
                    )
                if returned is None:
                    if not settings.allow_partial_coverage:
                        return None, CandidateFailure(
                            issue_codes=["RETURN_TRAVEL_DOES_NOT_FIT"],
                            message=(
                                "The required final journey does not fit before the allowed "
                                "end of the last day."
                            ),
                            affected_ids=[context.end_location],
                        )
                    # In partial-coverage mode, disclose the incomplete return
                    # instead of aborting the whole plan.
                    settings = _SearchSettings(
                        use_minimum_visit_duration=settings.use_minimum_visit_duration,
                        day_end_extension_minutes=settings.day_end_extension_minutes,
                        long_mode_index=settings.long_mode_index,
                        allow_partial_coverage=True,
                        relaxations=(
                            *settings.relaxations,
                            AppliedRelaxation(
                                step=RelaxationStep.OMIT_UNFITTABLE_STOP,
                                code="RETURN_LEG_OMITTED",
                                reason=(
                                    "The final return journey to the origin did not fit in "
                                    "the confirmed dates. Consider extending the trip by one "
                                    "day to include it."
                                ),
                                affected_ids=[context.end_location],
                                original_value=context.end_location,
                                relaxed_value="return_not_scheduled",
                            ),
                        ),
                    )
                else:
                    state = returned

            if not state.items:
                _add_free_time(state, context)
            days.append(
                CandidateDay(
                    day_number=state.day_number,
                    date=day_date,
                    start_location=state.start_location.label,
                    base_stop=state.base.label,
                    items=state.items,
                )
            )
            current_base = state.base

        if remaining_stops:
            if not settings.allow_partial_coverage:
                return None, CandidateFailure(
                    issue_codes=["REQUIRED_STOPS_DO_NOT_FIT"],
                    message=(
                        "Not every confirmed destination stop fits in the available dates. "
                        f"Last scheduler reason: {self._last_stop_rejection or 'not recorded.'}"
                    ),
                    affected_ids=[stop.name for stop in remaining_stops],
                )
            # Move any remaining unfit stops into the omitted list so they are
            # disclosed to the traveller instead of silently dropped.
            omitted_stops.extend(remaining_stops)
            remaining_stops.clear()

        if not covered_stops:
            # Even the aggressive rung could not fit a single stop. Return a
            # bounded failure so the traveller is not misled with an empty plan.
            return None, CandidateFailure(
                issue_codes=["NO_STOPS_FIT"],
                message=(
                    "No confirmed destination stop could fit in the available dates. "
                    f"Last scheduler reason: {self._last_stop_rejection or 'not recorded.'}"
                ),
                affected_ids=[stop.name for stop in omitted_stops],
            )

        omitted = [
            place.id
            for values in optional.values()
            for place in values
            if place.id not in visited_ids
        ]
        disclosed_relaxations: list[AppliedRelaxation] = list(settings.relaxations)
        for skipped in omitted_stops:
            disclosed_relaxations.append(
                AppliedRelaxation(
                    step=RelaxationStep.OMIT_UNFITTABLE_STOP,
                    code="STOP_OMITTED_FOR_FEASIBILITY",
                    reason=(
                        "This destination could not be visited within the confirmed "
                        "dates. Consider extending the trip or dropping another stop "
                        "to include it."
                    ),
                    affected_ids=[skipped.name],
                    original_value="scheduled",
                    relaxed_value="omitted",
                )
            )
        return (
            FeasibleCandidate(
                candidate_id=f"candidate_{uuid4().hex[:12]}",
                days=days,
                covered_stops=covered_stops,
                visited_place_ids=visited_ids,
                omitted_optional_place_ids=omitted,
                relaxations=disclosed_relaxations,
                preference_score=sum(
                    score_map.get(place_id, 0) for place_id in visited_ids
                ),
            ),
            None,
        )

    async def _try_stop(
        self,
        state: _DayState,
        stop: ResolvedStop,
        places: list[PlaceEvidence],
        context: PlanningContext,
        evidence: EvidenceBundle,
        long_mode: str,
        local_mode: str,
        use_minimum: bool,
    ) -> _DayState | None:
        trial = state.clone()
        stop_point = _stop_point(stop)
        if trial.current.label.casefold() != stop.name.casefold():
            routed = await self._append_route(
                trial, stop_point, long_mode, context, evidence
            )
            if routed is None:
                self._last_stop_rejection = self._last_stop_rejection or (
                    f"could not route from {state.current.label} to {stop.name}"
                )
                return None
            trial = routed
        trial.base = stop_point

        ordered_places = await optimized_place_order(
            stop_point,
            places,
            self.routes,
            local_mode,
        )
        for place in ordered_places:
            visited = await self._append_visit(
                trial,
                place,
                context,
                evidence,
                local_mode,
                use_minimum,
            )
            if visited is None:
                self._last_stop_rejection = self._last_stop_rejection or (
                    f"could not schedule {place.name} at {stop.name}"
                )
                return None
            trial = visited

        if trial.current.label.casefold() != stop.name.casefold():
            returned = await self._append_route(
                trial, stop_point, local_mode, context, evidence
            )
            if returned is None:
                self._last_stop_rejection = self._last_stop_rejection or (
                    f"could not return from {trial.current.label} to {stop.name}"
                )
                return None
            trial = returned
        return trial

    async def _try_optional_place(
        self,
        state: _DayState,
        place: PlaceEvidence,
        context: PlanningContext,
        evidence: EvidenceBundle,
        local_mode: str,
        use_minimum: bool,
    ) -> _DayState | None:
        trial = await self._append_visit(
            state.clone(),
            place,
            context,
            evidence,
            local_mode,
            use_minimum,
        )
        if trial is None:
            return None
        return await self._append_route(
            trial, state.base, local_mode, context, evidence
        )

    async def _append_visit(
        self,
        state: _DayState,
        place: PlaceEvidence,
        context: PlanningContext,
        evidence: EvidenceBundle,
        mode: str,
        use_minimum: bool,
    ) -> _DayState | None:
        place_point = RoutePoint(
            place.name,
            place.location,
            # Web-research fallback places do not always include coordinates.
            # Qualify their lookup with the resolved stop to prevent a common
            # place name from being geocoded in another city or country.
            query=_place_route_query(place),
        )
        routed = await self._append_route(state, place_point, mode, context, evidence)
        if routed is None:
            self._last_stop_rejection = self._last_stop_rejection or (
                f"could not route to {place.name}"
            )
            return None
        duration_evidence = next(
            (item for item in evidence.visit_durations if item.place_id == place.id),
            None,
        )
        duration = estimate_visit_duration(
            place,
            context,
            place.estimated_visit_minutes,
            duration_evidence,
        )
        if use_minimum:
            duration = duration.model_copy(
                update={"scheduled_minutes": duration.minimum_minutes}
            )
        timing = _scheduled_visit_window(place, routed, duration)
        if timing is None:
            self._last_stop_rejection = f"{place.name} has no visit window within its opening hours and the day window"
            return None
        block_start, block_end, duration = timing
        if not routed.meal_added and block_end > _lunch_deadline(routed):
            if not _add_lunch(routed, context):
                return None
            timing = _scheduled_visit_window(place, routed, duration)
            if timing is None:
                self._last_stop_rejection = (
                    f"{place.name} no longer fits after the required lunch break"
                )
                return None
            block_start, block_end, duration = timing
        if block_end > routed.end:
            self._last_stop_rejection = (
                f"{place.name} ends after the allowed day window"
            )
            return None
        routed.items.append(
            ScheduledVisit(
                start=block_start,
                end=block_end,
                place_id=place.id,
                duration=duration,
            )
        )
        routed.cursor = block_end
        routed.current = place_point
        routed.activity_count += 1
        # Add the meal / senior-rest break immediately AFTER the first activity,
        # so the requirement is visible even when the day fits only one visit.
        _add_due_breaks(routed, context)
        return routed

    async def _append_route(
        self,
        state: _DayState,
        destination: RoutePoint,
        mode: str,
        context: PlanningContext,
        evidence: EvidenceBundle | None = None,
    ) -> _DayState | None:
        if state.current.label.casefold() == destination.label.casefold():
            state.current = destination
            return state
        selected_mode = _mode_for_leg(
            state.current, destination, mode, context, evidence
        )
        try:
            route = await self.routes.get(state.current, destination, selected_mode)
        except Exception:
            route = None
        if (
            route is None
            or route.duration_minutes is None
            or route.status == FactStatus.UNAVAILABLE
        ):
            route = _local_coordinate_fallback(
                state, destination, context, selected_mode
            )
        if (
            route is None
            or route.duration_minutes is None
            or route.status == FactStatus.UNAVAILABLE
        ):
            self._last_stop_rejection = f"no usable {selected_mode} route from {state.current.label} to {destination.label}"
            return None
        if (
            route.distance_km is not None
            and route.distance_km <= SAME_LOCATION_DISTANCE_KM
        ):
            # The provider confirmed that both labels resolve to the same
            # physical location. Keep the provider evidence, but do not charge
            # time or money for a fictional drive between two names for it.
            state.current = destination
            return state
        window = calculate_travel_window(route.duration_minutes, route.mode)
        end = state.cursor + timedelta(minutes=window.planned_minutes)
        if (
            state.activity_count > 0
            and not state.meal_added
            and end > _lunch_deadline(state)
            and not _add_lunch(state, context)
        ):
            self._last_stop_rejection = (
                "the required lunch break did not fit before travel"
            )
            return None
        if state.meal_added:
            end = state.cursor + timedelta(minutes=window.planned_minutes)
        if end > state.end:
            if state.activity_count == 0:
                state.end = max(state.end, end)
            else:
                self._last_stop_rejection = (
                    f"travel to {destination.label} exceeds the remaining day window"
                )
                return None
        state.items.append(
            ScheduledTravel(
                start=state.cursor,
                end=end,
                route=route,
                window=window,
                origin_location=state.current.location or route.origin_location,
                destination_location=(
                    destination.location or route.destination_location
                ),
            )
        )
        state.cursor = end
        state.current = destination
        return state

    async def _try_transfer_day(
        self,
        state: _DayState,
        stop: ResolvedStop,
        mode: str,
        context: PlanningContext,
        evidence: EvidenceBundle,
    ) -> _DayState | None:
        trial = await self._append_route(
            state.clone(), _stop_point(stop), mode, context, evidence
        )
        if trial is not None:
            trial.base = _stop_point(stop)
        return trial


def _scheduled_visit_window(
    place: PlaceEvidence,
    state: _DayState,
    duration: VisitDuration,
) -> tuple[datetime, datetime, VisitDuration] | None:
    """Return the complete visible POI block, including operational time."""

    block_start = state.cursor
    core_earliest = block_start + timedelta(
        minutes=duration.access_minutes + duration.waiting_minutes
    )
    latest_core_end = state.end - timedelta(
        minutes=duration.rest_minutes + duration.exit_minutes
    )
    core_start = next_valid_visit_start(
        place,
        core_earliest,
        duration.scheduled_minutes,
        latest_core_end,
        duration.last_entry_minutes_before_close,
    )
    if core_start is None:
        return None
    opening_wait = max(
        0,
        round((core_start - core_earliest).total_seconds() / 60),
    )
    if opening_wait:
        waiting = duration.waiting_minutes + opening_wait
        duration = duration.model_copy(
            update={
                "waiting_minutes": waiting,
                "activity_buffer_minutes": (
                    duration.access_minutes
                    + waiting
                    + duration.rest_minutes
                    + duration.exit_minutes
                ),
            }
        )
    core_end = core_start + timedelta(minutes=duration.scheduled_minutes)
    block_end = core_end + timedelta(
        minutes=duration.rest_minutes + duration.exit_minutes
    )
    return block_start, block_end, duration


def _validated_places(
    evidence: EvidenceBundle,
    intent: PlannerIntent,
) -> tuple[list[PlaceEvidence], dict[str, int], CandidateFailure | None]:
    place_map = evidence.place_map()
    ranked_ids = [item.place_id for item in intent.ranked_places]
    unknown = sorted(set(ranked_ids) - set(place_map))
    if unknown:
        return (
            [],
            {},
            CandidateFailure(
                issue_codes=["UNKNOWN_RANKED_PLACE_ID"],
                message="The Planner Agent ranked place IDs that were not in verified evidence.",
                affected_ids=unknown,
            ),
        )
    if not ranked_ids:
        return (
            [],
            {},
            CandidateFailure(
                issue_codes=["NO_RANKED_PLACES"],
                message="No verified places were supplied for candidate generation.",
            ),
        )
    score_map = {item.place_id: item.score for item in intent.ranked_places}
    ordered = sorted(
        (place_map[place_id] for place_id in dict.fromkeys(ranked_ids)),
        key=lambda place: (-score_map[place.id], place.name.casefold()),
    )
    return ordered, score_map, None


def _place_queues(
    places: list[PlaceEvidence],
    stops: list[ResolvedStop],
    constraints: ConstraintBundle,
    score_map: dict[str, int],
) -> tuple[
    dict[str, list[PlaceEvidence]],
    dict[str, list[PlaceEvidence]],
    CandidateFailure | None,
]:
    by_stop: dict[str, list[PlaceEvidence]] = {}
    for place in places:
        by_stop.setdefault(place.stop_name.casefold(), []).append(place)

    required: dict[str, list[PlaceEvidence]] = {}
    optional: dict[str, list[PlaceEvidence]] = {}
    required_ids: set[str] = set()
    for stop in stops:
        key = stop.name.casefold()
        candidates = sorted(
            by_stop.get(key, []),
            key=lambda place: (-score_map.get(place.id, 0), place.name.casefold()),
        )
        if not candidates:
            return (
                {},
                {},
                CandidateFailure(
                    issue_codes=["STOP_HAS_NO_EXACT_POI"],
                    message=f"No exact verified POI is available for {stop.name}.",
                    affected_ids=[stop.name],
                ),
            )
        # A destination name (for example, "Goa") is not a separate
        # attraction. Treating it as a must-see substring would make every
        # place whose name contains it compulsory and frequently makes a
        # normal trip impossible to schedule.
        named_must_sees = [
            must
            for must in constraints.limits.must_see
            if must.casefold() != stop.name.casefold()
        ]
        must_matches = [
            place
            for place in candidates
            if any(must.casefold() in place.name.casefold() for must in named_must_sees)
        ]
        # Every destination needs one verified anchor.  Extra ranked places
        # are optional; otherwise a model ranking can accidentally turn a
        # short relaxed trip into several mandatory visits per stop.
        primary = next(
            (place for place in candidates if place.is_primary_destination),
            candidates[0],
        )
        core = _unique_places([*must_matches, primary])
        required[key] = core
        required_ids.update(place.id for place in core)
        optional[key] = [place for place in candidates if place.id not in required_ids]

    missing_must_see = [
        name
        for name in constraints.limits.must_see
        if not any(name.casefold() in place.name.casefold() for place in places)
    ]
    if missing_must_see:
        return (
            {},
            {},
            CandidateFailure(
                issue_codes=["MUST_SEE_EVIDENCE_MISSING"],
                message="A must-see place was not present in exact provider evidence.",
                affected_ids=missing_must_see,
            ),
        )
    return required, optional, None


def _search_settings(
    constraints: ConstraintBundle,
    intent: PlannerIntent,
) -> list[_SearchSettings]:
    settings = [_SearchSettings()]
    if constraints.policy.permits(RelaxationStep.USE_MINIMUM_VISIT_DURATION):
        settings.append(
            _SearchSettings(
                use_minimum_visit_duration=True,
                relaxations=(
                    AppliedRelaxation(
                        step=RelaxationStep.USE_MINIMUM_VISIT_DURATION,
                        code="VISITS_SHORTENED_TO_VERIFIED_MINIMUM",
                        reason="Recommended visit durations did not fit the confirmed dates.",
                        original_value="recommended duration",
                        relaxed_value="verified minimum duration",
                    ),
                ),
            )
        )
    faster_modes = _preferred_modes(
        intent.preferred_long_distance_modes,
        constraints.limits.allowed_long_distance_modes,
    )
    fastest_mode_index = _fastest_mode_index(faster_modes)
    if fastest_mode_index != 0 and constraints.policy.permits(
        RelaxationStep.USE_FASTER_ALLOWED_MODE
    ):
        settings.append(
            _SearchSettings(
                use_minimum_visit_duration=True,
                long_mode_index=fastest_mode_index,
                relaxations=(
                    AppliedRelaxation(
                        step=RelaxationStep.USE_MINIMUM_VISIT_DURATION,
                        code="VISITS_SHORTENED_TO_VERIFIED_MINIMUM",
                        reason="Recommended visit durations did not fit the confirmed dates.",
                        original_value="recommended duration",
                        relaxed_value="verified minimum duration",
                    ),
                    AppliedRelaxation(
                        step=RelaxationStep.USE_FASTER_ALLOWED_MODE,
                        code="FASTER_ALLOWED_MODE_USED",
                        reason="A faster user-allowed mode was needed for feasibility.",
                        original_value=faster_modes[0],
                        relaxed_value=faster_modes[fastest_mode_index],
                    ),
                ),
            )
        )
    # -----------------------------------------------------------------
    # Last-resort rung: keep every earlier relaxation AND permit skipping
    # a specific destination stop that simply cannot be fit. This is what
    # lets the planner always return SOMETHING useful for the traveller
    # rather than a "cannot plan" reply, while still disclosing exactly
    # which stops were omitted.
    # -----------------------------------------------------------------
    if constraints.policy.permits(RelaxationStep.OMIT_UNFITTABLE_STOP):
        settings.append(
            _SearchSettings(
                use_minimum_visit_duration=True,
                long_mode_index=fastest_mode_index,
                allow_partial_coverage=True,
                relaxations=(
                    AppliedRelaxation(
                        step=RelaxationStep.USE_MINIMUM_VISIT_DURATION,
                        code="VISITS_SHORTENED_TO_VERIFIED_MINIMUM",
                        reason="Recommended visit durations did not fit the confirmed dates.",
                        original_value="recommended duration",
                        relaxed_value="verified minimum duration",
                    ),
                ),
            )
        )
    return settings


def _preferred_modes(preferred: list[str], allowed: list[str]) -> list[str]:
    allowed_by_key = {mode.casefold(): mode for mode in allowed}
    result = [
        allowed_by_key[mode.casefold()]
        for mode in preferred
        if mode.casefold() in allowed_by_key
    ]
    result.extend(
        mode
        for mode in allowed
        if mode.casefold() not in {x.casefold() for x in result}
    )
    return result or ["car"]


def _mode_for_leg(
    origin: RoutePoint,
    destination: RoutePoint,
    preferred_mode: str,
    context: PlanningContext,
    evidence: EvidenceBundle | None = None,
) -> str:
    """Select the most practical allowed mode for this specific leg."""

    mode_key = preferred_mode.casefold()
    distance_km = None
    if origin.location is not None and destination.location is not None:
        distance_km = _point_distance_km(origin, destination)
    elif evidence is not None:
        matching_route = next(
            (
                r
                for r in evidence.routes
                if r.origin_name.casefold() == origin.label.casefold()
                and r.destination_name.casefold() == destination.label.casefold()
                and r.distance_km is not None
            ),
            None,
        )
        if matching_route is not None:
            distance_km = matching_route.distance_km

    if distance_km is None:
        return preferred_mode

    if mode_key in {"flight", "plane", "air"}:
        overland_mode = next(
            (
                mode
                for mode in context.long_distance_modes
                if mode.casefold()
                in {
                    "car",
                    "taxi",
                    "driving",
                    "self_driving",
                    "bus",
                    "coach",
                    "train",
                    "rail",
                }
            ),
            None,
        )
        if overland_mode is not None:
            max_overland_km = (
                650 if overland_mode.casefold() in {"train", "rail"} else 300
            )
            if distance_km <= max_overland_km:
                return overland_mode
        return preferred_mode

    allowed_modes = {
        mode.casefold()
        for mode in (context.long_distance_modes or ["flight", "train", "car"])
    }
    if distance_km > 800 and allowed_modes.intersection({"flight", "plane", "air"}):
        return "flight"

    return preferred_mode


def _point_distance_km(origin: RoutePoint, destination: RoutePoint) -> float:
    assert origin.location is not None and destination.location is not None
    lat1 = radians(origin.location.latitude)
    lat2 = radians(destination.location.latitude)
    delta_lat = lat2 - lat1
    delta_lon = radians(destination.location.longitude - origin.location.longitude)
    value = sin(delta_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(delta_lon / 2) ** 2
    return 2 * 6371 * asin(sqrt(value))


def _fastest_mode_index(modes: list[str]) -> int:
    """Return the fastest user-allowed mode without reordering their default."""

    speed_rank = {
        "flight": 0,
        "plane": 0,
        "air": 0,
        "train": 1,
        "rail": 1,
        "metro": 2,
        "car": 3,
        "taxi": 3,
        "scooter": 4,
        "driving": 4,
        "walking": 5,
        "walk": 5,
    }
    return min(
        range(len(modes)),
        key=lambda index: (speed_rank.get(modes[index].casefold(), 99), index),
    )


def _unique_places(values: list[PlaceEvidence]) -> list[PlaceEvidence]:
    result: list[PlaceEvidence] = []
    seen: set[str] = set()
    for place in values:
        if place.id not in seen:
            seen.add(place.id)
            result.append(place)
    return result


def _stop_point(stop: ResolvedStop) -> RoutePoint:
    from app.models.evidence import GeoPoint

    location = (
        GeoPoint(latitude=stop.latitude, longitude=stop.longitude)
        if stop.latitude is not None and stop.longitude is not None
        else None
    )
    return RoutePoint(stop.name, location)


def _place_route_query(place: PlaceEvidence) -> str:
    """Return an unambiguous provider query without changing the UI label."""

    place_key = place.name.casefold()
    stop_key = place.stop_name.casefold()
    if stop_key and stop_key not in place_key:
        return f"{place.name}, {place.stop_name}"
    return place.name


def _local_coordinate_fallback(
    state: _DayState,
    destination: RoutePoint,
    context: PlanningContext,
    mode: str,
) -> RouteEvidence | None:
    """Use a disclosed coordinate-based estimate when live routing is unavailable."""

    stop_names = {item.casefold() for item in context.destination.items}
    base_label = state.base.label.casefold()
    is_matching_stop = base_label in stop_names or any(
        base_label in s or s in base_label for s in stop_names
    )
    # Never invent a long route from text alone.  For a local transfer we can
    # use the existing conservative buffer, while a longer estimate requires
    # coordinates for both endpoints.
    if not is_matching_stop and (
        state.current.location is None or destination.location is None
    ):
        return None

    distance_km = 10.0
    duration_minutes = 30
    if state.current.location is not None and destination.location is not None:
        distance_km = _point_distance_km(state.current, destination)
        speed_kmh = (
            4.0
            if mode.casefold() in {"walking", "walk"}
            else 55.0
            if mode.casefold()
            in {
                "car",
                "taxi",
                "driving",
                "self_driving",
                "self-driving",
                "bus",
                "coach",
            }
            else 30.0
        )
        duration_minutes = max(15, round((distance_km / speed_kmh) * 60))

    return RouteEvidence(
        id=f"local-buffer-{state.base.label.casefold()}-{state.current.label.casefold()}-{destination.label.casefold()}",
        origin_name=state.current.label,
        destination_name=destination.label,
        mode=mode,
        distance_km=round(distance_km, 1),
        duration_minutes=duration_minutes,
        cost_basis="Coordinate-based estimated route; confirm the exact route before travel.",
        status=FactStatus.ESTIMATED,
    )


def _required_places_today(
    remaining_places: int,
    *,
    remaining_days: int,
    reserve_return_day: bool,
) -> int:
    """Reserve required POIs for later usable days instead of front-loading.

    This is deliberately conservative: arrival and return travel often use a
    day, but if a place can fit on either it is still allowed.  The result is
    at least one place and never exceeds the unresolved queue.
    """

    usable_days = max(1, remaining_days - (1 if reserve_return_day else 0))
    return max(1, min(remaining_places, ceil(remaining_places / usable_days)))


def _daily_activity_capacity(context: PlanningContext) -> int:
    base = {
        "relaxed": 2,
        "slow": 2,
        "moderate": 3,
        "packed": 4,
    }.get(context.travel_style.casefold(), 3)
    # Pilgrimage / circuit trips visit many short devotional stops per day, so
    # the deterministic capacity is raised to reflect the traveller intent.
    circuit_terms = ("ashtavinayak", "circuit", "pilgrimage", "temples")
    is_circuit = any(
        term in item.casefold()
        for item in context.destination.items
        for term in circuit_terms
    ) or any(
        term in interest.casefold()
        for interest in context.activity_interests
        for term in ("temple", "pilgrimage", "religious")
    )
    if is_circuit:
        base = max(base + 1, 3)
    return base


def _add_due_breaks(state: _DayState, context: PlanningContext) -> None:
    if not state.meal_added and state.cursor.time() >= time(12, 0):
        _add_lunch(state, context)

    # Keep the scheduler and final validator on the same contract.  A
    # traveller note such as "ample rest time" previously triggered the
    # validator but not the scheduler, so an otherwise valid plan was always
    # rejected at the final gate.
    needs_rest = _needs_rest_break(context)
    if needs_rest and not state.rest_added and state.activity_count >= 1:
        end = state.cursor + timedelta(minutes=30)
        if end <= state.end:
            state.items.append(
                ScheduledBreak(
                    kind="rest",
                    start=state.cursor,
                    end=end,
                    title="Planned rest and washroom break",
                    description="Reserved recovery time for a comfortable and unhurried day.",
                )
            )
            state.cursor = end
            state.rest_added = True


def _needs_rest_break(context: PlanningContext) -> bool:
    if context.num_seniors or context.accessibility_needs:
        return True
    pattern = re.compile(r"\b(rest|unhurried|queue)\b")
    return any(
        pattern.search(note.casefold()) is not None
        for note in context.trip_context_notes
    )


def _add_hotel_departure(state: _DayState) -> bool:
    """Reserve generic checkout and luggage-loading time at an overnight base."""

    start = state.cursor - timedelta(minutes=20)
    state.items.append(
        ScheduledBreak(
            kind="check_out",
            start=start,
            end=state.cursor,
            title=f"Hotel checkout and luggage handling in {state.base.label}",
            description=(
                "Allow time to settle the room, collect luggage, and load it before departure. "
                "Replace this estimate with the booked property's policy."
            ),
        )
    )
    return True


def _add_hotel_arrival(state: _DayState) -> bool:
    """Reserve generic check-in/luggage time after reaching a new overnight base."""

    end = state.cursor + timedelta(minutes=30)
    # Preferred sightseeing hours do not make a necessary late hotel arrival
    # impossible. Permit a small operational tail after the activity window.
    if end > state.end + timedelta(minutes=60):
        return False
    state.items.append(
        ScheduledBreak(
            kind="check_in",
            start=state.cursor,
            end=end,
            title=f"Hotel check-in, luggage drop, and settling in {state.base.label}",
            description=(
                "Allow time to reach the stay area, handle luggage, and settle in. "
                "Confirm early check-in or luggage storage with the booked property."
            ),
        )
    )
    state.cursor = end
    return True


def _is_transfer_only_day(state: _DayState) -> bool:
    """Long transfer buffers may already consume the full preferred day window."""

    return bool(state.items) and all(
        item.kind in {"transport", "check_out"} for item in state.items
    )


def _add_lunch(state: _DayState, context: PlanningContext) -> bool:
    """Insert an honest meal break inside the supported lunch window."""

    start = max(
        state.cursor,
        datetime.combine(state.date, _LUNCH_EARLIEST),
    )
    is_travel_day = any(item.kind == "transport" for item in state.items)
    latest_start = time(15, 30) if is_travel_day else _LUNCH_LATEST_START
    deadline = datetime.combine(state.date, latest_start)
    if start > deadline:
        if is_travel_day or state.cursor.time() >= time(15, 30):
            # On travel days arriving late afternoon, lunch was consumed during transit.
            state.meal_added = True
            return True
        return False
    end = start + timedelta(minutes=_LUNCH_DURATION_MINUTES)
    if end > state.end:
        if is_travel_day:
            state.meal_added = True
            return True
        return False
    has_dietary_restrictions = bool(context.dietary_restrictions)
    title = (
        "Lunch break — dietary options to confirm"
        if has_dietary_restrictions
        else "Flexible local lunch break"
    )
    state.items.append(
        ScheduledBreak(
            kind="meal",
            start=start,
            end=end,
            title=title,
            description=(
                "Choose a nearby restaurant and confirm dietary suitability before ordering."
                if has_dietary_restrictions
                else "Choose a nearby restaurant that suits your schedule and budget."
            ),
        )
    )
    state.cursor = end
    state.meal_added = True
    return True


def _lunch_deadline(state: _DayState) -> datetime:
    return datetime.combine(state.date, _LUNCH_LATEST_START)


def _add_free_time(state: _DayState, context: PlanningContext) -> None:
    duration = min(120, max(30, round((state.end - state.cursor).total_seconds() / 60)))
    end = min(state.end, state.cursor + timedelta(minutes=duration))
    state.items.append(
        ScheduledBreak(
            kind="free_time",
            start=state.cursor,
            end=end,
            title=f"Flexible time in {state.base.label}",
            description=(
                "This time is intentionally unplanned for rest, weather changes, or local choices."
            ),
        )
    )
