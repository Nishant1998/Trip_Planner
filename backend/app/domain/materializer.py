"""Turn an itinerary draft into a timed, costed TripPlan."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from math import asin, ceil, cos, radians, sin, sqrt
from urllib.parse import urlencode
from uuid import uuid4

from app.models.draft import DraftActivity, DraftCost, ItineraryDraft
from app.models.evidence import (
    CostBaselineEvidence,
    CurrencyRateEvidence,
    EvidenceBundle,
    FactStatus,
    PlaceEvidence,
    RouteEvidence,
    VisitDurationEvidence,
    WeatherEvidence,
)
from app.models.context import (
    DaySkeleton,
    PlanningContext,
    ResolvedDestination,
    TripSkeleton,
)
from app.models.plan import (
    BudgetSummary,
    DayPlan,
    GenerationMetadata,
    MoneyRange,
    PlaceRef,
    PlanItem,
    PlanStatus,
    StaySuggestion,
    TravelLeg,
    TripPlan,
    WeatherSummary,
)
from app.domain.closing_journey import (
    ClosingLeg,
    build_closing_legs,
    projected_closing_time,
)
from app.domain.cost_policy import apply_cost_policy
from app.domain.duration import estimate_visit_duration
from app.domain.expenses import category_totals, convert_cost, sum_money
from app.domain.meal_policy import align_meal_start
from app.domain.opening_hours import (
    hours_text_for_date,
    next_valid_visit_start,
)
from app.domain.safe_copy import (
    is_requested_shopping_time,
    safe_day_theme,
    safe_description,
    safe_plan_summary,
    safe_plan_title,
    safe_title,
)
from app.domain.stay_policy import (
    approximate_stay_area,
    approximate_stay_description,
)
from app.domain.travel_buffer import calculate_travel_window


def materialize_plan(
    *,
    context: PlanningContext,
    destination: ResolvedDestination,
    skeleton: TripSkeleton,
    draft: ItineraryDraft,
    evidence: EvidenceBundle,
    model_name: str,
    planner_version: str,
    request_id: str,
    repair_attempts: int,
    stage_latency_ms: dict[str, int],
    input_tokens: int = 0,
    output_tokens: int = 0,
    estimated_llm_cost_usd: Decimal = Decimal("0"),
) -> TripPlan:
    draft = apply_cost_policy(draft, context, destination, evidence, skeleton)
    place_map = evidence.place_map()
    route_map = {
        (route.origin_name.casefold(), route.destination_name.casefold()): route
        for route in evidence.routes
    }
    weather_map = {
        (weather.stop_name.casefold(), weather.date): weather
        for weather in evidence.weather
    }

    days: list[DayPlan] = []
    categorized: list[tuple[str, MoneyRange]] = []
    materializer_warnings: list[str] = []
    previous_base = context.origin

    draft_days = {day.day_number: day for day in draft.days}
    for day_skeleton in skeleton.days:
        draft_day = draft_days.get(day_skeleton.day_number)
        activities = list(draft_day.activities if draft_day else [])
        day, day_costs, warnings = _materialize_day(
            context=context,
            day_skeleton=day_skeleton,
            theme=safe_day_theme(context, day_skeleton.base_stop),
            activities=activities,
            place_map=place_map,
            route_map=route_map,
            weather_map=weather_map,
            evidence=evidence,
            previous_base=previous_base,
            return_target=(
                context.end_location
                if day_skeleton.day_number == len(skeleton.days)
                else None
            ),
        )
        days.append(day)
        categorized.extend(day_costs)
        materializer_warnings.extend(warnings)
        previous_base = day_skeleton.base_stop

    stays: list[StaySuggestion] = []
    for stay in draft.stays:
        converted = convert_cost(
            stay.cost, context.currency_code, evidence.currency_rates
        )
        categorized.append(("lodging", converted))
        stays.append(
            StaySuggestion(
                stop=stay.stop_name,
                area=approximate_stay_area(stay.stop_name),
                nights=stay.nights,
                description=approximate_stay_description(stay.stop_name),
                estimated_cost=converted,
                status=FactStatus.ESTIMATED,
                source_ids=converted.source_ids,
            )
        )

    totals = category_totals(categorized, context.currency_code)
    all_costs = [cost for _, cost in categorized]
    overall = sum_money(
        all_costs, context.currency_code, "Aggregated itinerary estimate."
    )
    budget = BudgetSummary(
        total_budget=context.trip_budget,
        estimated_minimum=overall.minimum,
        estimated_maximum=overall.maximum,
        contingency_reserved=skeleton.contingency_amount,
        currency_code=context.currency_code,
        within_budget=overall.maximum <= skeleton.spendable_budget,
        category_totals=totals,
    )

    initial_status = (
        PlanStatus.PARTIAL
        if any(status != "ok" for status in evidence.provider_status.values())
        else PlanStatus.COMPLETE
    )
    return TripPlan(
        plan_id=f"plan_{uuid4().hex[:16]}",
        trip_id=context.trip_id,
        status=initial_status,
        title=safe_plan_title(context, destination),
        summary=safe_plan_summary(context, destination),
        start_date=context.start_date,
        end_date=context.end_date,
        resolved_stops=[stop.name for stop in destination.stops],
        days=days,
        stays=stays,
        budget=budget,
        context_notes_applied=[
            note
            for note in context.trip_context_notes
            if note in draft.context_notes_applied
        ],
        assumptions=_dedupe(
            [
                *skeleton.assumptions,
                "The trip budget is assumed to include origin and return travel.",
                (
                    f"Lodging estimates assume approximately "
                    f"{max(1, ceil(context.traveller_count / 2))} rooms unless the "
                    "selected lodging type uses per-person beds."
                ),
                "Prices are estimates and do not represent bookings or live availability.",
                (
                    "Daily base routes currently use each resolved city centre, not a "
                    "booked hotel; recalculate routes after confirming accommodation."
                ),
            ]
        ),
        warnings=[
            *context.warnings,
            *_current_fact_warnings(evidence, context),
            *materializer_warnings,
        ],
        sources=evidence.sources,
        generation=GenerationMetadata(
            planner_version=planner_version,
            model=model_name,
            generated_at=datetime.now().astimezone(),
            request_id=request_id,
            repair_attempts=repair_attempts,
            provider_status=evidence.provider_status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_llm_cost_usd=estimated_llm_cost_usd,
            stage_latency_ms=stage_latency_ms,
        ),
    )


def _materialize_day(
    *,
    context: PlanningContext,
    day_skeleton: DaySkeleton,
    theme: str,
    activities: list[DraftActivity],
    place_map: dict[str, PlaceEvidence],
    route_map: dict[tuple[str, str], RouteEvidence],
    weather_map: dict[tuple[str, date], WeatherEvidence],
    evidence: EvidenceBundle,
    previous_base: str,
    return_target: str | None,
) -> tuple[DayPlan, list[tuple[str, MoneyRange]], list[str]]:
    current = datetime.combine(day_skeleton.date, day_skeleton.day_start_time)
    end = datetime.combine(day_skeleton.date, day_skeleton.day_end_time)
    items: list[PlanItem] = []
    categorized: list[tuple[str, MoneyRange]] = []
    warnings: list[str] = []
    current_stop = previous_base
    current_place: PlaceEvidence | None = None
    visited_stops: set[str] = {previous_base.casefold()}

    first_destination = (
        day_skeleton.stop_names[0]
        if day_skeleton.stop_names
        else day_skeleton.base_stop
    )
    if current_stop.casefold() != first_destination.casefold():
        route = _find_route(route_map, current_stop, first_destination)
        current, item, cost = _transport_item(
            current=current,
            origin=current_stop,
            destination=first_destination,
            route=route,
            context=context,
            item_id=f"d{day_skeleton.day_number}-transport-{len(items) + 1}",
            rates=evidence.currency_rates,
        )
        items.append(item)
        categorized.append(("transport", cost))
        if current > end:
            warnings.append(f"Travel to {first_destination} exceeded the day window.")
        current_stop = first_destination
        current_place = None
        visited_stops.add(first_destination.casefold())

    for activity in activities:
        checkpoint = (
            current,
            current_stop,
            current_place,
            set(visited_stops),
            len(items),
            len(categorized),
        )
        place = place_map.get(activity.evidence_place_id or "")
        activity_stop = place.stop_name if place is not None else current_stop
        if place is not None and activity_stop.casefold() != current_stop.casefold():
            if current_place is not None:
                local_route = _find_route(route_map, current_place.name, current_stop)
                current, local_return, local_cost = _local_transport_item(
                    current=current,
                    origin=current_place.name,
                    destination=current_stop,
                    route=local_route,
                    context=context,
                    item_id=f"d{day_skeleton.day_number}-transport-{len(items) + 1}",
                    rates=evidence.currency_rates,
                    cost_baseline=_cost_baseline(
                        evidence,
                        current_stop,
                        "local_transport",
                    ),
                )
                items.append(local_return)
                categorized.append(("transport", local_cost))
                current_place = None
            route = _find_route(route_map, current_stop, activity_stop)
            current, transport, cost = _transport_item(
                current=current,
                origin=current_stop,
                destination=activity_stop,
                route=route,
                context=context,
                item_id=f"d{day_skeleton.day_number}-transport-{len(items) + 1}",
                rates=evidence.currency_rates,
            )
            items.append(transport)
            categorized.append(("transport", cost))
            current_stop = activity_stop
            current_place = None
            visited_stops.add(activity_stop.casefold())
            if current >= end:
                (
                    current,
                    current_stop,
                    current_place,
                    visited_stops,
                ) = _restore_activity_checkpoint(checkpoint, items, categorized)
                warnings.append(f"Travel to {activity_stop} exceeded the day window.")
                break

        if place is not None:
            local_origin = (
                current_place.name if current_place is not None else current_stop
            )
            if local_origin.casefold() != place.name.casefold():
                fallback_distance = (
                    _place_distance_km(current_place, place)
                    if current_place is not None
                    else None
                )
                route = _find_route(route_map, local_origin, place.name)
                current, transport, cost = _local_transport_item(
                    current=current,
                    origin=local_origin,
                    destination=place.name,
                    route=route,
                    context=context,
                    item_id=f"d{day_skeleton.day_number}-transport-{len(items) + 1}",
                    fallback_distance_km=fallback_distance,
                    rates=evidence.currency_rates,
                    cost_baseline=_cost_baseline(
                        evidence,
                        day_skeleton.base_stop,
                        "local_transport",
                    ),
                )
                items.append(transport)
                categorized.append(("transport", cost))
                if current >= end:
                    (
                        current,
                        current_stop,
                        current_place,
                        visited_stops,
                    ) = _restore_activity_checkpoint(checkpoint, items, categorized)
                    warnings.append(
                        f"Local travel to {place.name} exceeded the day window."
                    )
                    break

        visit_duration = (
            estimate_visit_duration(
                place,
                context,
                activity.duration_minutes,
                _visit_duration_evidence(evidence, place.id),
            )
            if place is not None
            else None
        )
        scheduled_minutes = (
            visit_duration.scheduled_minutes
            if visit_duration is not None
            else activity.duration_minutes
        )
        current = align_meal_start(activity.kind, activity.title, current)
        if is_requested_shopping_time(activity, context) and _notes_request_evening(
            context
        ):
            evening_start = current.replace(hour=17, minute=0, second=0, microsecond=0)
            current = max(current, evening_start)
        if place is not None:
            open_start = next_valid_visit_start(
                place,
                current,
                scheduled_minutes,
                end,
            )
            if open_start is None:
                (
                    current,
                    current_stop,
                    current_place,
                    visited_stops,
                ) = _restore_activity_checkpoint(checkpoint, items, categorized)
                warnings.append(
                    f"Dropped {place.name!r} because it could not fit published hours."
                )
                continue
            if open_start > current:
                items.append(
                    PlanItem(
                        id=f"d{day_skeleton.day_number}-item-{len(items) + 1}",
                        kind="free_time",
                        start_time=current.time(),
                        end_time=open_start.time(),
                        start_datetime=current,
                        end_datetime=open_start,
                        title=f"Flexible time near {place.name}",
                        description=(
                            f"Keep this time flexible before {place.name} opens."
                        ),
                    )
                )
                current = open_start
        activity_end = current + timedelta(minutes=scheduled_minutes)
        activity_buffer = (
            visit_duration.activity_buffer_minutes if visit_duration is not None else 15
        )
        closing_legs = build_closing_legs(
            current_stop=current_stop,
            current_place_name=(
                place.name
                if place is not None
                else current_place.name
                if current_place is not None
                else None
            ),
            planned_stops=day_skeleton.stop_names,
            visited_stops=visited_stops,
            base_stop=day_skeleton.base_stop,
            return_target=return_target,
        )
        projected_end = projected_closing_time(
            activity_end + timedelta(minutes=activity_buffer),
            closing_legs,
            route_map,
            local_mode=_configured_local_mode(context),
            long_mode=_configured_mode(context),
        )
        if activity_end > end or projected_end > end:
            (
                current,
                current_stop,
                current_place,
                visited_stops,
            ) = _restore_activity_checkpoint(checkpoint, items, categorized)
            warnings.append(
                f"Dropped {activity.title!r} to preserve the required return by "
                f"{day_skeleton.day_end_time.strftime('%H:%M')}."
            )
            continue

        converted = (
            convert_cost(activity.cost, context.currency_code, evidence.currency_rates)
            if activity.cost
            else None
        )
        if converted is not None:
            categorized.append((_category_for_kind(activity.kind), converted))

        place_ref = _place_ref(place, day_skeleton.date) if place else None
        source_ids = list(place.source_ids) if place else []
        items.append(
            PlanItem(
                id=f"d{day_skeleton.day_number}-item-{len(items) + 1}",
                kind=_normalized_kind(activity.kind),
                start_time=current.time(),
                end_time=activity_end.time(),
                start_datetime=current,
                end_datetime=activity_end,
                title=safe_title(activity, place, current_stop, context),
                description=safe_description(activity, place, context),
                place=place_ref,
                visit_duration=visit_duration,
                estimated_cost=converted,
                source_ids=source_ids,
            )
        )
        current = activity_end + timedelta(minutes=activity_buffer)
        if place is not None:
            current_place = place

    closing_legs = build_closing_legs(
        current_stop=current_stop,
        current_place_name=current_place.name if current_place is not None else None,
        planned_stops=day_skeleton.stop_names,
        visited_stops=visited_stops,
        base_stop=day_skeleton.base_stop,
        return_target=return_target,
    )
    for leg in closing_legs:
        current, transport, cost = _closing_transport_item(
            leg=leg,
            current=current,
            context=context,
            route_map=route_map,
            evidence=evidence,
            base_stop=day_skeleton.base_stop,
            item_id=f"d{day_skeleton.day_number}-closing-{len(items) + 1}",
        )
        items.append(transport)
        categorized.append(("transport", cost))
        current_stop = leg.destination
        current_place = None
        visited_stops.add(leg.destination.casefold())
    if current > end:
        warnings.append("The required closing journey exceeded the day window.")

    weather = weather_map.get((day_skeleton.base_stop.casefold(), day_skeleton.date))
    weather_summary = (
        WeatherSummary(
            summary=weather.summary,
            minimum_temperature_c=weather.minimum_temperature_c,
            maximum_temperature_c=weather.maximum_temperature_c,
            precipitation_probability=weather.precipitation_probability,
            status=weather.status,
            source_ids=weather.source_ids,
        )
        if weather is not None
        else None
    )

    day_cost = sum_money(
        [cost for _, cost in categorized],
        context.currency_code,
        "Aggregated daily estimates.",
    )
    return (
        DayPlan(
            day_number=day_skeleton.day_number,
            date=day_skeleton.date,
            base_stop=day_skeleton.base_stop,
            theme=theme,
            weather=weather_summary,
            items=items,
            daily_cost=day_cost,
        ),
        categorized,
        warnings,
    )


def _find_route(
    route_map: dict[tuple[str, str], RouteEvidence],
    origin: str,
    destination: str,
) -> RouteEvidence | None:
    return route_map.get((origin.casefold(), destination.casefold()))


def _restore_activity_checkpoint(
    checkpoint: tuple[
        datetime,
        str,
        PlaceEvidence | None,
        set[str],
        int,
        int,
    ],
    items: list[PlanItem],
    categorized: list[tuple[str, MoneyRange]],
) -> tuple[datetime, str, PlaceEvidence | None, set[str]]:
    """Remove provisional travel when its activity cannot be scheduled."""

    current, current_stop, current_place, visited_stops, item_count, cost_count = (
        checkpoint
    )
    del items[item_count:]
    del categorized[cost_count:]
    return current, current_stop, current_place, visited_stops


def _closing_transport_item(
    *,
    leg: ClosingLeg,
    current: datetime,
    context: PlanningContext,
    route_map: dict[tuple[str, str], RouteEvidence],
    evidence: EvidenceBundle,
    base_stop: str,
    item_id: str,
) -> tuple[datetime, PlanItem, MoneyRange]:
    route = _find_route(route_map, leg.origin, leg.destination)
    if leg.kind == "local":
        return _local_transport_item(
            current=current,
            origin=leg.origin,
            destination=leg.destination,
            route=route,
            context=context,
            item_id=item_id,
            rates=evidence.currency_rates,
            cost_baseline=_cost_baseline(evidence, base_stop, "local_transport"),
        )
    return _transport_item(
        current=current,
        origin=leg.origin,
        destination=leg.destination,
        route=route,
        context=context,
        item_id=item_id,
        rates=evidence.currency_rates,
    )


def _transport_item(
    *,
    current: datetime,
    origin: str,
    destination: str,
    route: RouteEvidence | None,
    context: PlanningContext,
    item_id: str,
    rates: list[CurrencyRateEvidence],
) -> tuple[datetime, PlanItem, MoneyRange]:
    duration = int(route.duration_minutes if route and route.duration_minutes else 90)
    distance = float(route.distance_km if route and route.distance_km else 40)
    status = route.status if route else FactStatus.ESTIMATED
    source_ids = list(route.source_ids) if route else []
    mode = route.mode if route is not None else _configured_mode(context)
    travel_window = calculate_travel_window(duration, mode)
    end = current + timedelta(minutes=travel_window.planned_minutes)
    transport_cost = _long_transport_cost(
        route=route,
        distance_km=distance,
        mode=mode,
        traveller_count=context.traveller_count,
        target_currency=context.currency_code,
        rates=rates,
    )
    leg = TravelLeg(
        origin_label=origin,
        destination_label=destination,
        origin_location=route.origin_location if route else None,
        destination_location=route.destination_location if route else None,
        mode=mode,
        distance_km=distance,
        duration_minutes=duration,
        travel_window=travel_window,
        status=status,
        source_ids=source_ids,
    )
    item = PlanItem(
        id=f"{item_id}-{_short_slug(destination)}",
        kind="transport",
        start_time=current.time(),
        end_time=end.time(),
        start_datetime=current,
        end_datetime=end,
        title=f"Travel from {origin} to {destination}",
        description="Provider travel time is shown separately from planning buffers.",
        travel_from_previous=leg,
        estimated_cost=transport_cost,
        source_ids=source_ids,
    )
    return end, item, transport_cost


def _local_transport_item(
    *,
    current: datetime,
    origin: str,
    destination: str,
    route: RouteEvidence | None,
    context: PlanningContext,
    item_id: str,
    rates: list[CurrencyRateEvidence],
    cost_baseline: CostBaselineEvidence | None,
    fallback_distance_km: float | None = None,
) -> tuple[datetime, PlanItem, MoneyRange]:
    mode = route.mode if route else _configured_local_mode(context)
    distance = float(
        route.distance_km
        if route and route.distance_km is not None
        else fallback_distance_km or 5
    )
    duration = int(
        route.duration_minutes if route and route.duration_minutes is not None else 30
    )
    travel_window = calculate_travel_window(duration, mode)
    end = current + timedelta(minutes=travel_window.planned_minutes)
    cost = _local_transport_cost(
        distance_km=distance,
        mode=mode,
        traveller_count=context.traveller_count,
        target_currency=context.currency_code,
        rates=rates,
        baseline=cost_baseline,
        route_source_ids=list(route.source_ids) if route else [],
    )
    leg = TravelLeg(
        origin_label=origin,
        destination_label=destination,
        origin_location=route.origin_location if route else None,
        destination_location=route.destination_location if route else None,
        mode=mode,
        distance_km=round(distance, 1),
        duration_minutes=duration,
        travel_window=travel_window,
        status=route.status if route else FactStatus.UNAVAILABLE,
        source_ids=list(route.source_ids) if route else [],
    )
    item = PlanItem(
        id=f"{item_id}-{_short_slug(destination)}",
        kind="transport",
        start_time=current.time(),
        end_time=end.time(),
        start_datetime=current,
        end_datetime=end,
        title=f"Local travel from {origin} to {destination}",
        description=(
            "Local route time is shown separately from uncertainty and access buffers."
        ),
        travel_from_previous=leg,
        estimated_cost=cost,
        source_ids=list(route.source_ids) if route else [],
    )
    return end, item, cost


def _place_ref(place: PlaceEvidence, visit_date: date) -> PlaceRef:
    return PlaceRef(
        id=place.id,
        provider_place_id=place.provider_place_id,
        name=place.name,
        stop_name=place.stop_name,
        address=place.address,
        image_url=place.image_url,
        google_maps_url=_google_maps_url(place),
        opening_hours=hours_text_for_date(place, visit_date),
        latitude=place.location.latitude if place.location else None,
        longitude=place.location.longitude if place.location else None,
        status=place.status,
        hours_status=(
            FactStatus.VERIFIED if place.opening_periods else FactStatus.UNAVAILABLE
        ),
        source_ids=place.source_ids,
    )


def _google_maps_url(place: PlaceEvidence) -> str | None:
    if not place.provider_place_id:
        return None
    query = ", ".join(
        value for value in (place.name, place.address, place.stop_name) if value
    )
    return "https://www.google.com/maps/search/?" + urlencode(
        {
            "api": "1",
            "query": query,
            "query_place_id": place.provider_place_id,
        }
    )


def _normalized_kind(kind: str) -> str:
    allowed = {
        "transport",
        "activity",
        "meal",
        "rest",
        "check_in",
        "check_out",
        "free_time",
    }
    return kind if kind in allowed else "activity"


def _category_for_kind(kind: str) -> str:
    return {
        "meal": "food",
        "transport": "transport",
        "check_in": "lodging",
        "check_out": "lodging",
    }.get(kind, "activities")


def _short_slug(value: str) -> str:
    return "-".join(value.casefold().replace(",", " ").split())[:40]


def _configured_mode(context: PlanningContext) -> str:
    return context.long_distance_modes[0] if context.long_distance_modes else "car"


def _configured_local_mode(context: PlanningContext) -> str:
    return context.local_transport_modes[0] if context.local_transport_modes else "car"


def _long_transport_cost(
    *,
    route: RouteEvidence | None,
    distance_km: float,
    mode: str,
    traveller_count: int,
    target_currency: str,
    rates: list[CurrencyRateEvidence],
) -> MoneyRange:
    if (
        route is not None
        and route.minimum_cost is not None
        and route.maximum_cost is not None
        and route.cost_currency_code
        and route.cost_basis
    ):
        return convert_cost(
            DraftCost(
                minimum=float(route.minimum_cost),
                maximum=float(route.maximum_cost),
                currency_code=route.cost_currency_code,
                basis=(
                    f"{route.cost_basis} This is a mode-level range, not a live fare."
                ),
                source_ids=route.source_ids,
            ),
            target_currency,
            rates,
        )
    minimum, maximum, basis = _transport_cost(
        distance_km,
        mode,
        traveller_count,
        target_currency,
    )
    return MoneyRange(
        minimum=minimum,
        maximum=maximum,
        currency_code=target_currency,
        status=FactStatus.ESTIMATED,
        basis=basis,
        source_ids=list(route.source_ids) if route else [],
    )


def _local_transport_cost(
    *,
    distance_km: float,
    mode: str,
    traveller_count: int,
    target_currency: str,
    rates: list[CurrencyRateEvidence],
    baseline: CostBaselineEvidence | None,
    route_source_ids: list[str],
) -> MoneyRange:
    if baseline is not None:
        distance_units = max(Decimal("1"), Decimal(str(distance_km)) / Decimal("5"))
        converted = convert_cost(
            DraftCost(
                minimum=float(baseline.minimum * distance_units),
                maximum=float(baseline.maximum * distance_units),
                currency_code=baseline.currency_code,
                basis=(
                    f"Current local-transport baseline ({baseline.unit}), scaled "
                    "conservatively by route distance; not a live quote."
                ),
                source_ids=list(
                    dict.fromkeys([*baseline.source_ids, *route_source_ids])
                ),
            ),
            target_currency,
            rates,
        )
        return converted
    minimum, maximum, basis = _transport_cost(
        distance_km,
        mode,
        traveller_count,
        target_currency,
    )
    return MoneyRange(
        minimum=minimum,
        maximum=maximum,
        currency_code=target_currency,
        status=FactStatus.ESTIMATED,
        basis=(
            f"{basis} No source-backed local cost baseline was available; "
            "confirm locally."
        ),
        source_ids=route_source_ids,
    )


def _cost_baseline(
    evidence: EvidenceBundle,
    stop_name: str,
    category: str,
) -> CostBaselineEvidence | None:
    return next(
        (
            baseline
            for baseline in evidence.cost_baselines
            if baseline.stop_name.casefold() == stop_name.casefold()
            and baseline.category.casefold() == category.casefold()
        ),
        None,
    )


def _notes_request_evening(context: PlanningContext) -> bool:
    notes = " ".join(context.trip_context_notes).casefold()
    return "evening" in notes and any(
        word in notes for word in ("shopping", "market", "bazaar")
    )


def _visit_duration_evidence(
    evidence: EvidenceBundle,
    place_id: str,
) -> VisitDurationEvidence | None:
    return next(
        (
            duration
            for duration in evidence.visit_durations
            if duration.place_id == place_id
        ),
        None,
    )


def _transport_cost(
    distance_km: float,
    mode: str,
    traveller_count: int,
    currency_code: str,
) -> tuple[Decimal, Decimal, str]:
    distance = Decimal(str(distance_km))
    travellers = Decimal(max(1, traveller_count))
    normalized = mode.casefold()
    if normalized in {"flight", "plane", "air"}:
        minimum = max(Decimal("5000"), distance * Decimal("2.5")) * travellers
        maximum = max(Decimal("9000"), distance * Decimal("4.5")) * travellers
        basis = f"Estimated flight cost for {traveller_count} travellers."
    elif normalized in {"train", "rail", "metro"}:
        minimum = max(Decimal("300"), distance * Decimal("2")) * travellers
        maximum = max(Decimal("1000"), distance * Decimal("6")) * travellers
        basis = f"Estimated rail cost for {traveller_count} travellers."
    elif normalized in {"walking", "walk"}:
        minimum = Decimal("0")
        maximum = Decimal("0")
        basis = "Walking cost estimate."
    else:
        minimum = distance * Decimal("8")
        maximum = distance * Decimal("15")
        basis = "Estimated whole-vehicle ground transport cost."
    scale = {
        "INR": Decimal("1"),
        "JPY": Decimal("1.72"),
        "USD": Decimal("0.0116"),
        "EUR": Decimal("0.0100"),
        "GBP": Decimal("0.0086"),
    }.get(currency_code.upper(), Decimal("0.0116"))
    return (
        (minimum * scale).quantize(Decimal("0.01")),
        (maximum * scale).quantize(Decimal("0.01")),
        f"{basis} Converted from the declared INR MVP policy to {currency_code}.",
    )


def _place_distance_km(origin: PlaceEvidence, destination: PlaceEvidence) -> float:
    if origin.location is None or destination.location is None:
        return 2.0
    latitude_1 = radians(origin.location.latitude)
    latitude_2 = radians(destination.location.latitude)
    latitude_delta = radians(destination.location.latitude - origin.location.latitude)
    longitude_delta = radians(
        destination.location.longitude - origin.location.longitude
    )
    haversine = (
        sin(latitude_delta / 2) ** 2
        + cos(latitude_1) * cos(latitude_2) * sin(longitude_delta / 2) ** 2
    )
    direct_distance = 2 * 6371 * asin(sqrt(haversine))
    return max(0.5, direct_distance * 1.25)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.strip().casefold()
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _current_fact_warnings(
    evidence: EvidenceBundle,
    context: PlanningContext,
) -> list[str]:
    return [
        (
            f"Confirm current {fact.fact_type} for {fact.entity_name} before travel; "
            "use the linked source."
        )
        for fact in evidence.web_facts
        if fact.applicable_date is None
        or context.start_date <= fact.applicable_date <= context.end_date
    ]
