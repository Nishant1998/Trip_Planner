"""Render a feasible schedule into the stable public TripPlan contract."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from math import ceil
from urllib.parse import urlencode
from uuid import uuid4

from app.models.agent import ToolCallAudit
from app.models.candidate import (
    FeasibleCandidate,
    ScheduledBreak,
    ScheduledTravel,
    ScheduledVisit,
)
from app.models.constraints import RelaxationStep
from app.models.evidence import EvidenceBundle, FactStatus, PlaceEvidence
from app.models.context import PlanningContext, ResolvedDestination
from app.models.preferences import PlannerIntent
from app.models.plan import (
    BudgetSummary,
    DayPlan,
    DeliveryStatus,
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
from app.domain.candidate_costs import (
    activity_cost,
    meal_cost,
    route_cost,
    stay_cost,
    zero_cost,
)
from app.domain.expenses import category_totals, sum_money
from app.domain.opening_hours import hours_text_for_date
from app.domain.safe_copy import safe_day_theme, safe_plan_summary, safe_plan_title
from app.domain.stay_policy import approximate_stay_area, approximate_stay_description


def render_candidate(
    *,
    candidate: FeasibleCandidate,
    context: PlanningContext,
    destination: ResolvedDestination,
    evidence: EvidenceBundle,
    intent: PlannerIntent,
    model_name: str,
    planner_version: str,
    request_id: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    estimated_llm_cost_usd: Decimal = Decimal("0"),
    stage_latency_ms: dict[str, int] | None = None,
    agent_tool_calls: list[ToolCallAudit] | None = None,
) -> TripPlan:
    """Use only candidate times and evidence facts; no LLM prose is accepted here."""

    place_map = evidence.place_map()
    categorized: list[tuple[str, MoneyRange]] = []
    days: list[DayPlan] = []
    for candidate_day in candidate.days:
        day_items: list[PlanItem] = []
        day_costs: list[MoneyRange] = []
        for index, item in enumerate(candidate_day.items, start=1):
            plan_item, category, cost = _render_item(
                item=item,
                item_id=f"d{candidate_day.day_number}-item-{index}",
                base_stop=candidate_day.base_stop,
                context=context,
                evidence=evidence,
                place_map=place_map,
            )
            day_items.append(plan_item)
            day_costs.append(cost)
            categorized.append((category, cost))

        day_cost = sum_money(
            day_costs,
            context.currency_code,
            "Aggregated deterministic daily estimate.",
        )
        days.append(
            DayPlan(
                day_number=candidate_day.day_number,
                date=candidate_day.date,
                base_stop=candidate_day.base_stop,
                theme=safe_day_theme(context, candidate_day.base_stop),
                weather=_weather(evidence, candidate_day.base_stop, candidate_day.date),
                items=day_items,
                daily_cost=day_cost,
            )
        )

    stays = _render_stays(candidate, context, evidence)
    for stay in stays:
        categorized.append(("lodging", stay.estimated_cost))
    totals = category_totals(categorized, context.currency_code)
    total = sum_money(
        [cost for _, cost in categorized],
        context.currency_code,
        "Aggregated route, activity, meal, and lodging estimates.",
    )
    contingency = (context.trip_budget * Decimal("0.10")).quantize(Decimal("0.01"))
    spendable = context.trip_budget - contingency
    # A displayed "within budget" result must be literally true.  The UI may
    # still show the full estimate range, but it must not reassure a traveller
    # when the conservative maximum can exceed their stated budget.
    within_budget = total.minimum <= spendable and total.maximum <= context.trip_budget
    applied_notes = [
        context.trip_context_notes[directive.note_index]
        for directive in intent.note_directives
        if directive.note_index < len(context.trip_context_notes)
    ]
    already_covered_notes = [
        context.trip_context_notes[acknowledgement.note_index]
        for acknowledgement in intent.note_acknowledgements
        if acknowledgement.disposition == "already_covered"
        and acknowledgement.note_index < len(context.trip_context_notes)
    ]
    unapplied_note_warnings = [
        (
            "Trip note could not be applied: "
            f"{context.trip_context_notes[acknowledgement.note_index]} "
            f"Reason: {acknowledgement.reason}"
        )
        for acknowledgement in intent.note_acknowledgements
        if acknowledgement.disposition != "already_covered"
        and acknowledgement.note_index < len(context.trip_context_notes)
    ]
    return TripPlan(
        schema_version="1.4",
        plan_id=f"plan_{uuid4().hex[:16]}",
        trip_id=context.trip_id,
        status=(
            PlanStatus.PARTIAL
            if any(
                relaxation.step == RelaxationStep.OMIT_UNFITTABLE_STOP
                for relaxation in candidate.relaxations
            )
            else PlanStatus.COMPLETE
        ),
        delivery_status=(
            DeliveryStatus.READY_WITH_RELAXATIONS
            if candidate.relaxations
            else DeliveryStatus.READY
        ),
        title=safe_plan_title(context, destination),
        summary=safe_plan_summary(context, destination),
        start_date=context.start_date,
        end_date=context.end_date,
        resolved_stops=[stop.name for stop in destination.stops],
        days=days,
        stays=stays,
        budget=BudgetSummary(
            total_budget=context.trip_budget,
            estimated_minimum=total.minimum,
            estimated_maximum=total.maximum,
            contingency_reserved=contingency,
            currency_code=context.currency_code,
            within_budget=within_budget,
            category_totals=totals,
        ),
        context_notes_applied=_unique([*applied_notes, *already_covered_notes]),
        assumptions=[
            "Routes use resolved stop centres until a specific hotel is booked.",
            "Prices are planning ranges, not bookings or live availability.",
            (
                f"Lodging assumes approximately {max(1, ceil(context.traveller_count / 2))} "
                "room(s), unless a later booking uses another arrangement."
            ),
        ],
        warnings=[
            *(relaxation.reason for relaxation in candidate.relaxations),
            *unapplied_note_warnings,
            *_provider_warnings(evidence),
        ],
        relaxations=candidate.relaxations,
        sources=evidence.sources,
        generation=GenerationMetadata(
            planner_version=planner_version,
            model=model_name,
            generated_at=datetime.now().astimezone(),
            request_id=request_id,
            repair_attempts=0,
            provider_status=evidence.provider_status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_llm_cost_usd=estimated_llm_cost_usd,
            stage_latency_ms=stage_latency_ms or {},
            agent_tool_calls=agent_tool_calls or [],
        ),
    )


def _render_item(
    *,
    item: ScheduledTravel | ScheduledVisit | ScheduledBreak,
    item_id: str,
    base_stop: str,
    context: PlanningContext,
    evidence: EvidenceBundle,
    place_map: dict[str, PlaceEvidence],
) -> tuple[PlanItem, str, MoneyRange]:
    if isinstance(item, ScheduledTravel):
        cost = route_cost(item.route, context, evidence, base_stop)
        leg = TravelLeg(
            origin_label=item.route.origin_name,
            destination_label=item.route.destination_name,
            origin_location=item.origin_location,
            destination_location=item.destination_location,
            mode=item.route.mode,
            distance_km=item.route.distance_km,
            duration_minutes=item.route.duration_minutes,
            travel_window=item.window,
            status=item.route.status,
            source_ids=item.route.source_ids,
        )
        return (
            PlanItem(
                id=item_id,
                kind="transport",
                start_time=item.start.time(),
                end_time=item.end.time(),
                start_datetime=item.start,
                end_datetime=item.end,
                title=(
                    f"Travel from {item.route.origin_name} to "
                    f"{item.route.destination_name}"
                ),
                description=(
                    "Provider travel time plus deterministic access and uncertainty buffers."
                ),
                travel_from_previous=leg,
                estimated_cost=cost,
                source_ids=item.route.source_ids,
            ),
            "transport",
            cost,
        )

    if isinstance(item, ScheduledVisit):
        place = place_map[item.place_id]
        cost = activity_cost(place, context, evidence)
        return (
            PlanItem(
                id=item_id,
                kind="activity",
                start_time=item.start.time(),
                end_time=item.end.time(),
                start_datetime=item.start,
                end_datetime=item.end,
                title=place.name,
                description=(
                    f"Visit the exact provider-matched place. The schedule also reserves "
                    f"{item.duration.access_minutes} minutes for access, "
                    f"{item.duration.waiting_minutes} minutes for possible waiting, and "
                    f"{item.duration.rest_minutes} minutes for recovery, and "
                    f"{item.duration.exit_minutes} minutes for exit before the next journey."
                    + (
                        " This visit time is provisional; confirm the place's opening "
                        "hours before travel."
                        if not place.opening_periods
                        else ""
                    )
                ),
                place=_place_ref(place, item.start),
                visit_duration=item.duration,
                estimated_cost=cost,
                source_ids=place.source_ids,
            ),
            "activities",
            cost,
        )

    cost = (
        meal_cost(base_stop, context, evidence)
        if item.kind == "meal"
        else zero_cost(
            context.currency_code, "No direct cost is assigned to this time block."
        )
    )
    return (
        PlanItem(
            id=item_id,
            kind=item.kind,
            start_time=item.start.time(),
            end_time=item.end.time(),
            start_datetime=item.start,
            end_datetime=item.end,
            title=item.title,
            description=item.description,
            estimated_cost=cost,
            source_ids=cost.source_ids,
        ),
        "food" if item.kind == "meal" else "flexible_time",
        cost,
    )


def _place_ref(place: PlaceEvidence, visit_time: datetime) -> PlaceRef:
    return PlaceRef(
        id=place.id,
        provider_place_id=place.provider_place_id,
        name=place.name,
        stop_name=place.stop_name,
        address=place.address,
        image_url=place.image_url,
        google_maps_url=place.google_maps_uri or _exact_google_maps_url(place),
        opening_hours=hours_text_for_date(place, visit_time.date()),
        latitude=place.location.latitude if place.location else None,
        longitude=place.location.longitude if place.location else None,
        status=place.status,
        hours_status=place.status if place.opening_periods else FactStatus.UNAVAILABLE,
        source_ids=place.source_ids,
    )


def _exact_google_maps_url(place: PlaceEvidence) -> str | None:
    if not place.provider_place_id:
        return None
    query = ", ".join(value for value in (place.name, place.address) if value)
    return "https://www.google.com/maps/search/?" + urlencode(
        {"api": "1", "query": query, "query_place_id": place.provider_place_id}
    )


def _weather(
    evidence: EvidenceBundle,
    stop_name: str,
    visit_date: date,
) -> WeatherSummary | None:
    match = next(
        (
            value
            for value in evidence.weather
            if value.stop_name.casefold() == stop_name.casefold()
            and value.date == visit_date
        ),
        None,
    )
    if match is None:
        return None
    return WeatherSummary(
        summary=match.summary,
        minimum_temperature_c=match.minimum_temperature_c,
        maximum_temperature_c=match.maximum_temperature_c,
        precipitation_probability=match.precipitation_probability,
        status=match.status,
        source_ids=match.source_ids,
    )


def _provider_warnings(evidence: EvidenceBundle) -> list[str]:
    """Expose optional-provider gaps without implying a fact was retrieved."""

    if evidence.provider_status.get("weather") == "unavailable":
        return ["Weather information is unavailable, so no forecast has been included."]
    return []


def _render_stays(
    candidate: FeasibleCandidate,
    context: PlanningContext,
    evidence: EvidenceBundle,
) -> list[StaySuggestion]:
    excluded = {context.origin.casefold()}
    if context.end_location:
        excluded.add(context.end_location.casefold())
    nights = Counter(
        day.base_stop
        for day in candidate.days[:-1]
        if day.base_stop.casefold() not in excluded
    )
    result: list[StaySuggestion] = []
    for stop_name, night_count in nights.items():
        cost = stay_cost(stop_name, night_count, context, evidence)
        result.append(
            StaySuggestion(
                stop=stop_name,
                area=approximate_stay_area(stop_name),
                nights=night_count,
                description=approximate_stay_description(stop_name),
                estimated_cost=cost,
                status=FactStatus.ESTIMATED,
                source_ids=cost.source_ids,
            )
        )
    return result


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
