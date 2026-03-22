"""Six LLM roles sharing one bounded catalog and run-local evidence ledger."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.models.progress import PlanningStage
from app.planning_tools.contracts import (
    BookingOption,
    HotelSearchInput,
    MapLookupInput,
    Photo,
    Place,
    PlaceLookupInput,
    RouteLeg,
    TransportOption,
    TransportSearchInput,
    WeatherInput,
    WeatherReport,
)
from app.providers.tool_loop import AgentTool, AgentToolInputError, ToolCallingModel
from app.services.progress import ProgressReporter
from pydantic import BaseModel, ValidationError

from .agent_io import (
    ItineraryOutput,
    OptionsOutput,
    PresentationOutput,
    ResearchOutput,
    RoutingDecision,
)
from .contracts import (
    AccommodationRecord,
    DestinationRecord,
    Evidence,
    FactState,
    PlaceRecord,
    PlannedDay,
    PlannedEvent,
    PlanWarning,
    TransportRecord,
    WeatherRecord,
)
from .prompts import COMMON, PROMPTS, VERSION
from .budget import spending_targets
from .identity import city_points, resolve_label
from .daywise import DAILY_TIME_POLICY, day_workload, local_agent_tools, road_options


@dataclass(frozen=True)
class RoleSpec:
    prompt: str
    output: type[BaseModel]
    tools: tuple[str, ...]
    stage: PlanningStage
    state_keys: tuple[str, ...]


ROLE_SPECS = {
    "supervisor": RoleSpec(
        PROMPTS["supervisor"],
        RoutingDecision,
        (),
        PlanningStage.STARTING,
        ("agent_status", "routing", "validation", "warnings"),
    ),
    "destination_research": RoleSpec(
        PROMPTS["destination_research"],
        ResearchOutput,
        ("places", "weather"),
        PlanningStage.FINDING_PLACES,
        ("destinations", "places", "weather", "validation"),
    ),
    "itinerary_planning": RoleSpec(
        PROMPTS["itinerary_planning"],
        ItineraryOutput,
        (),
        PlanningStage.BUILDING_ITINERARY,
        (
            "destinations",
            "places",
            "weather",
            "stay_requests",
            "hotels",
            "journeys",
            "transport_options",
            "route_matrix",
            "route_requirements",
            "itinerary",
            "validation",
        ),
    ),
    "accommodation": RoleSpec(
        PROMPTS["accommodation"],
        OptionsOutput,
        ("accommodation",),
        PlanningStage.ESTIMATING_COSTS,
        ("destinations", "stay_requests", "hotels", "validation", "pending_jobs"),
    ),
    "transport_booking": RoleSpec(
        PROMPTS["transport_booking"],
        OptionsOutput,
        ("transport", "maps"),
        PlanningStage.CHECKING_ROUTES,
        (
            "destinations",
            "places",
            "hotels",
            "journeys",
            "transport_options",
            "route_requirements",
            "route_matrix",
            "validation",
            "pending_jobs",
        ),
    ),
    "response_aggregator": RoleSpec(
        PROMPTS["response_aggregator"],
        PresentationOutput,
        (),
        PlanningStage.FINALIZING,
        ("itinerary", "places", "hotels", "transport_options", "costs", "warnings", "validation"),
    ),
}
TOOL_INPUTS = {
    "places": PlaceLookupInput,
    "weather": WeatherInput,
    "maps": MapLookupInput,
    "transport": TransportSearchInput,
    "accommodation": HotelSearchInput,
}
TOOL_NAMES = {
    "places": "places_lookup",
    "weather": "weather_forecast",
    "maps": "maps_lookup",
    "transport": "transport_search",
    "accommodation": "accommodation_search",
}
TOOL_DESCRIPTIONS = {
    "places": "One Places action: text, nearby, details, or photo. Preserve attribution. Maximum 10 results.",
    "weather": "Forecast for coordinates and dates (at most 31 days); historical estimate may replace missing forecast.",
    "maps": "One directions or directed matrix lookup, at most 100 pairs. Not ticket inventory.",
    "transport": "One requested flight/train/bus search. Use continuation token to poll, never recollect pending jobs.",
    "accommodation": "Dated stay for exact guests/rooms; prices can be partial. Resume with continuation token.",
}


def json_value(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    return value


def planning_value(value):
    """Keep display evidence in the ledger, not in every planning prompt."""
    value = json_value(value)
    if isinstance(value, dict):
        return {key: planning_value(item) for key, item in value.items()
                if key not in {"photos", "photo_names", "image_refs", "display_photos"}}
    if isinstance(value, list):
        return [planning_value(item) for item in value]
    return value


def validate_tool_scope(name, query, state, entries):
    """Reject factual and party changes at the tool boundary, before spending."""
    request = state["user_preferences"]
    places = {p.id: p for p in state["places"]}
    for tool, _, response in entries:
        if tool == "places":
            for place in (
                response.data if isinstance(response.data, list) else [response.data]
            ):
                if isinstance(place, Place):
                    places[place.place_id] = place
    known = {key: value.location for key, value in places.items() if value.location}
    known.update(
        {
            h.selected.id: h.selected.location
            for h in state["hotels"]
            if h.selected and h.selected.location
        }
    )
    if name == "maps":
        points = (query.origins or [query.origin]) + (
            query.destinations or [query.destination]
        )
        if any(p.id not in known or known[p.id] != p.location for p in points):
            raise AgentToolInputError(
                "Maps requires source-backed IDs and coordinates."
            )
    if name == "weather" and (
        query.location not in known.values()
        or query.start_date < request.start_date
        or query.end_date > request.end_date
    ):
        raise AgentToolInputError(
            "Weather must use sourced coordinates and the requested trip date range."
        )
    if name == "places" and query.action == "details" and query.place_id not in places:
        raise AgentToolInputError("Use a place ID returned by a prior search.")
    if name == "places" and query.action == "photo":
        refs = [ref.name for p in places.values() for ref in p.photos]
        if query.photo_name not in refs:
            raise AgentToolInputError("Use a photo reference returned by details.")
    if name == "accommodation":
        labels = {d.id: d.label for d in state["destinations"]}
        matches = [
            s
            for s in state["stay_requests"]
            if labels.get(s.destination_id) == query.destination
            and s.check_in == query.check_in
            and s.check_out == query.check_out
            and s.adults == query.adults == request.adults
            and s.children == query.children == request.children
            and s.rooms == query.rooms
        ]
        if not matches:
            raise AgentToolInputError(
                "Search must match an allocated stay and confirmed party."
            )
    if name == "transport":
        legs = query.legs or [query]
        for leg in legs:
            day = getattr(leg, "departure_date", None) or query.departure_time.date()
            if not any(
                j.origin == leg.origin
                and j.destination == leg.destination
                and j.mode == query.mode
                and j.departure_date == day
                for j in state["journeys"]
            ):
                raise AgentToolInputError("Transport must match an allocated journey.")
        expected = (
            request.adults
            if query.mode == "flight"
            else request.adults + request.children
        )
        if query.passengers != expected or (
            query.mode == "flight" and query.children != request.children
        ):
            raise AgentToolInputError("Transport party must match confirmed travelers.")
        if query.infants_in_seat or query.infants_on_lap:
            raise AgentToolInputError(
                "Separate infant occupancy was not confirmed; do not invent it."
            )
        if query.trip_type == "Round trip" and not any(
            j.origin == query.destination
            and j.destination == query.origin
            and j.departure_date == query.return_date
            and j.mode == query.mode
            for j in state["journeys"]
        ):
            raise AgentToolInputError(
                "Return date and endpoints must match an allocated return journey."
            )
    tokens = (
        [query.continuation_token] if getattr(query, "continuation_token", None) else []
    )
    tokens += list(getattr(query, "continuation_tokens", {}).values())
    known_tokens = {r.continuation_token for _, _, r in entries if r.continuation_token}
    known_tokens.update(
        t for _, _, r in entries for t in r.continuation_tokens.values()
    )
    for pending in state.get("pending_jobs", {}).values():
        if isinstance(pending, dict):
            if pending.get("continuation_token"):
                known_tokens.add(pending["continuation_token"])
            known_tokens.update(pending.get("continuation_tokens", {}).values())
    if any(t not in known_tokens for t in tokens):
        raise AgentToolInputError("Only resume snapshot tokens returned in this run.")


class SixAgentRuntime:
    def __init__(
        self, model: ToolCallingModel, catalog, progress: ProgressReporter, journal=None
    ):
        self.model, self.catalog, self.progress, self.journal = (
            model,
            catalog,
            progress,
            journal,
        )
        self.entries: list[tuple[str, BaseModel, Any]] = []
        self.pending: dict[str, Any] = {}
        self.usage: list[dict] = []
        self.route_results: dict = {}

    async def invoke(self, role, state, **extra):
        spec = ROLE_SPECS[role]
        remaining = (state["run"].deadline_at - datetime.now(UTC)).total_seconds()
        reserve = 5 if role == "response_aggregator" else 30
        if remaining <= reserve:
            raise TimeoutError("planning deadline reached")
        calls = self.catalog.agent_tools()
        tools = []
        for name in spec.tools:

            async def execute(query, name=name):
                validate_tool_scope(name, query, state, self.entries)
                seconds = (
                    state["run"].deadline_at - datetime.now(UTC)
                ).total_seconds() - 30
                # Bright Data allows a 65s collection. Do not start it in the final reserve.
                required = 65 if name in ("transport", "accommodation") else 15
                if seconds < required:
                    raise TimeoutError("insufficient time for provider operation")
                result = await calls[name](query)
                self.entries.append(
                    (name, query.model_copy(deep=True), result.model_copy(deep=True))
                )
                if result.continuation_token or result.continuation_tokens:
                    self.pending[f"{name}:{len(self.entries)}"] = {
                        "tool": name,
                        "query": json_value(query),
                        "continuation_token": result.continuation_token,
                        "continuation_tokens": result.continuation_tokens,
                    }
                reply = planning_value(result)
                if name == "maps":
                    reply["journey_guidance"] = planning_value(road_options(state, self.entries))
                return reply

            tools.append(
                AgentTool(
                    TOOL_NAMES[name],
                    TOOL_DESCRIPTIONS[name],
                    TOOL_INPUTS[name],
                    execute,
                    spec.stage,
                    allow_repeated_calls=name in ("transport", "accommodation"),
                )
            )
        tools.extend(local_agent_tools(role, state, self.entries, self.route_results))
        request = state["user_preferences"].model_dump(
            mode="json", exclude={"confirmed_memory_json"}
        )
        payload = {
            "requirements": request,
            "state": {k: planning_value(state.get(k)) for k in spec.state_keys},
            "remaining_seconds": max(0, int(remaining - reserve)),
            "tool_limits": self.catalog.run_guard.limits,
            "prompt_version": VERSION,
            "planning_profile": state["run"].planning_profile,
            **extra,
        }
        if role == "transport_booking":
            payload["journey_guidance"] = planning_value(road_options(state, self.entries))
        if state["run"].planning_profile != "full":
            payload["spending_targets"] = json_value(spending_targets(state["user_preferences"]))
            payload["daily_time_policy"] = dict(DAILY_TIME_POLICY)
            payload["daily_capacity"] = [
                {"day": d.day, "date": str(d.date), "base_location": d.base_location,
                 **day_workload(d.model_copy(update={"events": []}), state)}
                for d in state["itinerary"]
            ]
        if state["run"].planning_profile != "full" and role == "transport_booking":
            endpoint = request["preferences"].get("end_location")
            endpoint = request["origin"] if endpoint == "origin" else endpoint
            labels = list(dict.fromkeys([request["origin"], *request["destinations"], *([endpoint] if endpoint else [])]))
            payload["resolved_city_points"] = [dict(planning_value(p), requested_label=label)
                                               for label, p in city_points(self.entries, labels).items()]
        if state["routing"].repair_started and role != "response_aggregator":
            owners = {
                "destination_research": {"research"},
                "itinerary_planning": {"allocation", "schedule"},
                "accommodation": {"accommodation"},
                "transport_booking": {"transport"},
            }.get(role, set())
            payload["repair_issues"] = [
                issue.model_dump(mode="json") for issue in state["validation"]
                if issue.owner in owners and issue.severity == "error"
            ]
        # Every invocation has an independent role conversation, not shared response IDs.
        async with asyncio.timeout(remaining - reserve):
            for attempt in range(2):
                try:
                    result = await self.model.run(
                        agent_name=role,
                        instructions=COMMON + spec.prompt + (
                            "\nDAYWISE PROFILE OVERRIDES hourly scheduling: use no POI route_requirements or exact event times. "
                            "Use calculate for arithmetic. Every activity needs a source place_id, an individual reason, "
                            "and a positive labelled visit_minutes allowance. Default active day is 09:00-17:00, 480 minutes: "
                            "reserve 120 for meals/rest, 90 for local movement and 30 contingency, then subtract city transfers. "
                            "Explicit user day windows override the default. Use daily_capacity when supplied; never exceed it. "
                            "Choose by total visit minutes, not a fixed POI count: usually two major visits or three short nearby visits. "
                              "Use planning_allowance durations (for example 20-45 for an exterior viewpoint, 60 for a short landmark, 120 for a museum, "
                            "180 or more for a large site); adapt to interests, accessibility and site size and explain the assumption. "
                            "Provider-verified visit durations are not yet supported; never claim those defaults are sourced facts. "
                            "Unknown transfer time means unverified capacity, not zero travel. Move overflowing activities to "
                            "another requested day in the same city, or explicitly report an infeasible mandatory visit; never drop it silently. "
                            "Use travel-only days for long journeys and allow recovery after overnight travel. "
                            "Preserve required city order. Research may also resolve city names including origin/endpoint "
                            "for transport, but choose activities only in requested destinations. Transport must call "
                            "optimize_route after collecting dated offers and compare its profiles; identical profiles are valid. "
                            "Route results optimize supplied offers only. Unknown accessibility requires explicit warning. "
                            "For car journeys use Maps DRIVE with resolved_city_points IDs; never call ticket search for car. "
                            "Return reasons for justified rest/transfer days; never invent facts to fill time."
                            if state["run"].planning_profile != "full" else ""
                        ),
                        input_payload=copy.deepcopy(payload),
                        tools=tools,
                        output_model=spec.output,
                        max_tool_calls=20,
                        progress=self.progress,
                        turn_stage=lambda: spec.stage,
                    )
                    output = spec.output.model_validate(result.output)
                    self.usage.append(
                        {
                            "role": role,
                            "input_tokens": result.usage.input_tokens,
                            "output_tokens": result.usage.output_tokens,
                        }
                    )
                    return output
                except ValidationError:
                    if attempt:
                        raise
                    payload["correction"] = (
                        "Previous output did not match the required schema. Return valid structured output."
                    )

    def warnings(self, role, output):
        warnings = [
            PlanWarning(
                code=f"{role}_limitation",
                source=role,
                message=text,
                affected_id=str(index),
            )
            for index, text in enumerate(getattr(output, "warnings", []))
        ]
        for index, (name, _, result) in enumerate(self.entries):
            if name in ROLE_SPECS[role].tools:
                warnings.extend(
                    PlanWarning(
                        code=result.error_code or f"{name}_partial",
                        source=name,
                        message=text,
                        affected_id=f"{index}:{n}",
                    )
                    for n, text in enumerate(result.warnings)
                )
        return warnings

    async def supervisor(self, state, eligible):
        if len(eligible) == 1:
            return RoutingDecision(task=eligible[0], reason="Only one dependency-safe task remains.")
        return await self.invoke("supervisor", state, eligible_tasks=eligible)

    async def research(self, state):
        output = await self.invoke("destination_research", state)
        found, weather = {}, []
        for name, query, result in self.entries:
            evidence = [
                Evidence(
                    provider=result.provider,
                    retrieved_at=result.retrieved_at,
                    status=FactState.VERIFIED
                    if result.status == "ok" and result.provider != "synthetic_level1"
                    else FactState.ESTIMATED,
                )
            ]
            if name == "places":
                data = result.data if isinstance(result.data, list) else [result.data]
                for place in data:
                    if isinstance(place, Place):
                        prior = found.get(place.place_id)
                        if prior:
                            updates = {field: getattr(place, field) for field in Place.model_fields
                                       if getattr(place, field) is not None
                                       and (field not in {"photos", "photo_names"} or getattr(place, field))}
                            place = prior[0].model_copy(update=updates)
                            place_evidence = [*prior[1], *evidence]
                        else:
                            place_evidence = evidence
                        found[place.place_id] = (place, place_evidence)
            if name == "weather" and isinstance(result.data, WeatherReport):
                weather.append((query, result, evidence))
        destinations = [
            DestinationRecord(id=f"d{index}", label=label)
            for index, label in enumerate(state["user_preferences"].destinations)
        ]
        places, reports = [], []
        for choice in output.choices:
            destination = next(
                (d for d in destinations if d.label == resolve_label(choice.destination, [item.label for item in destinations])), None
            )
            if destination is None or choice.place_id not in found:
                continue
            place, evidence = found[choice.place_id]
            photos = []
            for name, query, result in self.entries:
                if name == "places" and isinstance(result.data, Photo):
                    ref = next(
                        (ref for ref in place.photos if ref.name == query.photo_name),
                        None,
                    )
                    if ref:
                        photos.append(
                            result.data.model_copy(
                                update={"attribution": ref.attribution}
                            )
                        )
            places.append(
                PlaceRecord(
                    id=place.place_id,
                    destination_id=destination.id,
                    name=place.name,
                    selection_reason=choice.reason,
                    location=place.location,
                    address=place.address,
                    categories=place.types,
                    opening_hours=place.opening_hours,
                    business_status=place.business_status,
                    image_refs=place.photo_names,
                    photos=place.photos,
                    display_photos=photos,
                    map_url=place.map_url,
                    status=FactState.VERIFIED if all(e.status == FactState.VERIFIED and e.provider != "synthetic_level1" for e in evidence) else FactState.ESTIMATED,
                    evidence=evidence,
                )
            )
            for query, result, weather_evidence in weather:
                if query.location == place.location and not any(
                    r.destination_id == destination.id for r in reports
                ):
                    reports.append(
                        WeatherRecord(
                            destination_id=destination.id,
                            report=result.data,
                            status=FactState.VERIFIED
                            if result.status == "ok"
                            else FactState.ESTIMATED,
                            evidence=weather_evidence,
                            warnings=result.warnings,
                        )
                    )
        warnings = self.warnings("destination_research", output)
        if not places:
            warnings.append(
                PlanWarning(
                    code="no_places",
                    source="destination_research",
                    message="No source-backed places available.",
                )
            )
        return {
            "destinations": destinations,
            "places": places,
            "weather": reports,
            "warnings": warnings,
        }

    async def itinerary(self, state, task):
        output = await self.invoke("itinerary_planning", state, task=task)
        days = []
        request = state["user_preferences"]
        endpoint = request.preferences.end_location
        endpoint = request.origin if endpoint and endpoint.casefold() == "origin" else endpoint
        labels = list(dict.fromkeys([request.origin, *request.destinations, *([endpoint] if endpoint else [])]))
        places = {p.id: p for p in state["places"]}
        routes = {(r.origin_id, r.destination_id, r.mode.upper()): r
                  for r in state["route_matrix"]}
        modes = {
            (r.origin_id, r.destination_id): {
                "driving": "DRIVE", "walking": "WALK", "bicycling": "BICYCLE",
                "transit": "TRANSIT",
            }[r.mode] for r in state["route_requirements"]
        }
        for day in output.days:
            events, previous = [], None
            for item in day.events:
                place = places.get(item.place_id)
                event = PlannedEvent(
                    **item.model_dump(),
                    timing_status="provisional"
                    if item.start and item.end
                    else "unscheduled",
                )
                if place:
                    event = event.model_copy(
                        update={
                            "title": place.name,
                            "location": place.location,
                            "address": place.address,
                            "opening_hours": place.opening_hours,
                            "image_refs": place.image_refs,
                            "photos": place.photos,
                            "display_photos": place.display_photos,
                            "map_url": place.map_url,
                            "evidence": place.evidence,
                            "travel_from_previous": routes.get((
                                previous, place.id,
                                modes.get((previous, place.id), "DRIVE"),
                            ))
                            if previous
                            else None,
                        }
                    )
                    previous = place.id
                events.append(event)
            days.append(
                PlannedDay(
                    day=day.day,
                    date=state["user_preferences"].start_date
                    + timedelta(days=day.day - 1),
                    base_location=resolve_label(day.base_location, labels) or day.base_location,
                    title=day.title,
                    summary=day.summary,
                    events=events,
                )
            )
        update = {
            "itinerary": days,
            "route_requirements": output.route_requirements,
            "warnings": self.warnings("itinerary_planning", output),
        }
        journeys = [j.model_copy(update={
            "origin": resolve_label(j.origin, labels) or j.origin,
            "destination": resolve_label(j.destination, labels) or j.destination,
        }) for j in output.journeys]
        if task == "allocation":
            update.update(stay_requests=output.stays, journeys=journeys)
        elif state["routing"].repair_started:
            if output.stays:
                update["stay_requests"] = output.stays
            if output.journeys:
                update["journeys"] = journeys
        return update

    async def accommodation(self, state):
        output = await self.invoke("accommodation", state)
        records = []
        for stay in state["stay_requests"]:
            if (state["routing"].repair_started
                    and not any(s.request_id == stay.id for s in output.selections)):
                continue
            destination = next(
                (d.label for d in state["destinations"] if d.id == stay.destination_id),
                None,
            )
            options = {}
            for name, query, result in self.entries:
                if (
                    name == "accommodation"
                    and query.destination == destination
                    and query.check_in == stay.check_in
                    and query.check_out == stay.check_out
                    and query.adults == stay.adults
                    and query.children == stay.children
                    and query.rooms == stay.rooms
                ):
                    options.update(
                        {
                            o.id: o
                            for o in result.data or []
                            if isinstance(o, BookingOption)
                        }
                    )
            selection = next(
                (s for s in output.selections if s.request_id == stay.id), None
            )
            selected = options.get(selection.option_id) if selection else None
            evidence = [Evidence(provider=result.provider, retrieved_at=result.retrieved_at,
                        status=FactState.ESTIMATED if result.status != "ok" else FactState.VERIFIED)
                        for name, query, result in self.entries if name == "accommodation"
                        and query.destination == destination and query.check_in == stay.check_in
                        and query.check_out == stay.check_out and selected
                        and any(o.id == selected.id for o in result.data or [])]
            records.append(
                AccommodationRecord(
                    evidence=evidence,
                    stay_request=stay,
                    selected=selected,
                    alternatives=[
                        options[i] for i in selection.alternative_ids if i in options
                    ][:10]
                    if selection
                    else [],
                    status=(FactState.VERIFIED if evidence and all(e.status == FactState.VERIFIED and e.provider != "synthetic_level1" for e in evidence) else FactState.ESTIMATED) if selected else FactState.UNAVAILABLE,
                    reason=selection.reason if selection else "No supported selection.",
                )
            )
        return {"hotels": records, "pending_jobs": copy.deepcopy(self.pending),
                "warnings": self.warnings("accommodation", output)}

    async def transport(self, state):
        output = await self.invoke("transport_booking", state)
        labels = list(dict.fromkeys(label for j in state["journeys"] for label in (j.origin, j.destination)))
        sourced_cities = city_points(self.entries, labels)
        records, routes = [], {}
        for journey in state.get("journeys", []):
            if (state["routing"].repair_started
                    and not any(s.request_id == journey.id for s in output.selections)):
                continue
            options = {}
            for name, query, result in self.entries:
                if name != "transport" or query.mode != journey.mode:
                    continue
                for index, leg in enumerate(query.legs or [query]):
                    departure = getattr(leg, "departure_date", None) or (
                        query.departure_time.date() if query.departure_time else None
                    )
                    if (
                        leg.origin == journey.origin
                        and leg.destination == journey.destination
                        and departure == journey.departure_date
                    ):
                        options.update(
                            {
                                o.id: o
                                for o in result.data or []
                                if isinstance(o, TransportOption)
                                and (not query.legs or o.leg_index == index)
                            }
                        )
            if journey.mode == "car":
                options.update({o.id: o for o in road_options(state, self.entries).get(journey.id, [])})
            selection = next(
                (s for s in output.selections if s.request_id == journey.id), None
            )
            selected = options.get(selection.option_id) if selection else None
            evidence = [Evidence(provider=result.provider, retrieved_at=result.retrieved_at,
                        status=FactState.ESTIMATED if result.status != "ok" else FactState.VERIFIED)
                        for name, query, result in self.entries if name == "transport" and selected
                        and any(o.id == selected.id for o in result.data or [])]
            if journey.mode == "car" and selected:
                origin = sourced_cities.get(journey.origin)
                destination = sourced_cities.get(journey.destination)
                evidence = [Evidence(
                    provider=result.provider, retrieved_at=result.retrieved_at,
                    reference=f"{origin.place_id}->{destination.place_id}", status=FactState.ESTIMATED,
                ) for name, query, result in self.entries if name == "maps" and origin and destination
                    and any(isinstance(leg, RouteLeg) and leg.status == "ok"
                            and leg.mode.upper() == "DRIVE"
                            and leg.origin_id == origin.place_id and leg.destination_id == destination.place_id
                            and leg.duration_s == selected.duration_s
                            for leg in (result.data if isinstance(result.data, list) else [result.data]))]
            records.append(
                TransportRecord(
                    evidence=evidence,
                    id=journey.id,
                    origin=journey.origin,
                    destination=journey.destination,
                    mode=journey.mode,
                    selected=selected,
                    alternatives=[
                        options[i] for i in selection.alternative_ids if i in options
                    ][:10]
                    if selection
                    else [],
                    status=(FactState.VERIFIED if evidence and all(e.status == FactState.VERIFIED and e.provider != "synthetic_level1" for e in evidence) else FactState.ESTIMATED) if selected else FactState.UNAVAILABLE,
                    reason=selection.reason if selection else "No supported selection.",
                )
            )
        for name, query, result in self.entries:
            if name == "maps":
                for leg in (
                    result.data if isinstance(result.data, list) else [result.data]
                ):
                    if isinstance(leg, RouteLeg):
                        routes[(leg.origin_id, leg.destination_id, leg.mode)] = leg
        return {
            "transport_options": records,
            "route_profiles": copy.deepcopy(self.route_results),
            "route_matrix": list(routes.values()),
            "pending_jobs": copy.deepcopy(self.pending),
            "warnings": self.warnings("transport_booking", output),
        }

    async def presentation(self, state):
        return await self.invoke("response_aggregator", state)
