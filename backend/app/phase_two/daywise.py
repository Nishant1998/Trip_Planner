"""Local calculation/route adapters and coarse daily checks; no agent decisions."""
import asyncio
from datetime import UTC, time

from app.models.progress import PlanningStage
from app.planning_tools.calculator import CalculatorInput, calculate
from app.planning_tools.contracts import Place, RouteLeg, TransportOption
from app.planning_tools.route_optimizer import optimize_route
from app.providers.tool_loop import AgentTool
from pydantic import BaseModel, ConfigDict

from .contracts import ValidationIssue

DAILY_TIME_POLICY = {
    "version": "daily_capacity.v1",
    "default_envelope_minutes": 480,
    "meals_rest_minutes": 120,
    "local_movement_minutes": 90,
    "contingency_minutes": 30,
    "basis": "Editable planning assumptions; local movement is not a measured POI route.",
}


def arithmetic(expression, **variables):
    result = calculate({"expression": expression, "variables": variables})
    if result["status"] != "ok":
        raise ValueError("Planner arithmetic failed")
    return float(result["result"])


def day_workload(day, state):
    request = state["user_preferences"]
    prefs = request.preferences
    envelope = DAILY_TIME_POLICY["default_envelope_minutes"]
    opens, closes = time(9), time(17)
    # Only explicit user windows override the default; inferred/default fields do not.
    if (request.preference_sources.get("day_start_time") == "user"
            and request.preference_sources.get("day_end_time") == "user"
            and prefs.day_start_time and prefs.day_end_time):
        start, end = prefs.day_start_time, prefs.day_end_time
        envelope = max(0, (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute))
        opens, closes = start, end
    activities = [e for e in day.events if e.kind == "activity"]
    missing = [e.id for e in activities if not e.visit_minutes or e.duration_basis == "unknown"]
    visits = arithmetic("sum(v)", v=[e.visit_minutes for e in activities if e.visit_minutes is not None] or [0])
    transfer, overnight = 0, []
    local_start, local_end = opens.hour * 60 + opens.minute, closes.hour * 60 + closes.minute
    selected = {r.id: r.selected for r in state["transport_options"]}
    for journey in state["journeys"]:
        option = selected.get(journey.id)
        dep, arr = (option.departure, option.arrival) if option else (None, None)
        if dep and dep.utcoffset() is not None and dep.date() == day.date and journey.origin == day.base_location:
            local_end = min(local_end, dep.hour * 60 + dep.minute)
        if arr and arr.utcoffset() is not None and journey.destination == day.base_location:
            if arr.date() == day.date:
                local_start = max(local_start, arr.hour * 60 + arr.minute)
            elif journey.departure_date == day.date and arr.date() > day.date:
                local_end = local_start
        if (dep and arr and dep.utcoffset() is not None and arr.utcoffset() is not None
                and arr.astimezone(UTC) > dep.astimezone(UTC) and dep.date() != arr.date()):
            if dep.date() <= day.date <= arr.date():
                overnight.append(journey.id)
                # Conservative local-clock portions, not timezone-correct elapsed duration.
                if day.date == dep.date():
                    part = 1440 - dep.hour * 60 - dep.minute
                elif day.date == arr.date():
                    part = arr.hour * 60 + arr.minute
                else:
                    part = 1440
                transfer += part
            continue
        if journey.departure_date != day.date:
            continue
        if option and option.duration_s is not None:
            transfer += arithmetic("seconds / 60", seconds=option.duration_s)
        else:
            missing.append(journey.id)
    buffer = DAILY_TIME_POLICY["meals_rest_minutes"]
    local = DAILY_TIME_POLICY["local_movement_minutes"]
    contingency = DAILY_TIME_POLICY["contingency_minutes"]
    reserved = arithmetic("meals + local + slack", meals=buffer, local=local, slack=contingency)
    residence = max(0, local_end - local_start)
    capacity_bound = min(envelope - reserved - transfer, residence - reserved)
    transfer_unknown = any(j.id in missing for j in state["journeys"])
    remaining_bound = arithmetic("capacity - visits", capacity=capacity_bound, visits=visits)
    remaining = None if missing else remaining_bound
    # A long travel-only day is valid; its negative allowance means no sightseeing.
    # Known durations alone can prove overload even if other components are missing.
    status = "overloaded" if remaining_bound < 0 and activities else "unknown" if missing else "travel_only" if remaining_bound < 0 else "within_allowance"
    return {"policy_version": DAILY_TIME_POLICY["version"],
            "envelope_minutes": envelope, "buffer_minutes": buffer,
            "local_movement_minutes": local, "contingency_minutes": contingency,
            "poi_capacity_minutes": None if transfer_unknown else max(0, capacity_bound),
            "visit_minutes": visits if not any(e.id in missing for e in activities) else None,
            "transfer_minutes": None if transfer_unknown else transfer,
            "remaining_minutes": remaining, "status": status, "unknown_components": missing,
            "overnight_journeys": overnight,
            "city_available_minutes": residence,
            "basis": "Planning allowance, not an hourly schedule: meals/rest 120 min, local movement 90 min, contingency 30 min. Default active window 09:00-17:00; explicit user windows override it. Overnight portions are conservative local-clock allowances. Unknown durations prevent confirmation."}


def daywise_issues(state, issues):
    # Preserve full-schedule findings visibly, without asking a day-wise agent to
    # repair intentionally absent hourly schedules or POI routing.
    informational = {"timing_unknown", "route_missing", "meal_missing", "rest_missing",
                     "journey_time_unknown", "travel_window_unverified", "hours_unknown"}
    result = [i.model_copy(update={"severity": "warning"}) if i.code in informational else i for i in issues]
    destinations = {d.id: d.label for d in state["destinations"]}
    places = {p.id: p for p in state["places"]}
    for day in state["itinerary"]:
        day.workload = day_workload(day, state)
        if day.workload["status"] == "overloaded":
            result.append(ValidationIssue(code="day_overloaded", message="Visits and transfers exceed the daily allowance.", affected_id=str(day.day)))
        elif day.workload["status"] == "unknown":
            result.append(ValidationIssue(code="workload_unknown", message="Missing durations prevent workload verification.", affected_id=str(day.day), severity="warning"))
        for event in day.events:
            if event.kind != "activity":
                continue
            if not event.reason.strip():
                result.append(ValidationIssue(code="choice_reason_missing", message="Explain why this activity matches the trip.", affected_id=event.id))
            place = places.get(event.place_id)
            if not place or not place.evidence:
                result.append(ValidationIssue(code="activity_evidence_missing", message="Activity needs returned place evidence.", affected_id=event.id))
            elif destinations.get(place.destination_id) != day.base_location:
                result.append(ValidationIssue(code="activity_city_conflict", message="Activity is not in this day's city.", affected_id=event.id))
    return result


class OptimizeInput(BaseModel):
    """Facts come from the runtime evidence ledger, never model-supplied fares."""
    model_config = ConfigDict(extra="forbid")


def road_options(state, entries):
    """Directed sourced road durations; no inferred fares or invented services."""
    from .identity import city_points
    labels = list(dict.fromkeys(label for j in state["journeys"] for label in (j.origin, j.destination)))
    names = {place.place_id: label for label, place in city_points(entries, labels).items()}
    options = {}
    for name, query, result in entries:
        if name != "maps":
            continue
        for leg in result.data if isinstance(result.data, list) else [result.data]:
            if not isinstance(leg, RouteLeg) or leg.mode.upper() != "DRIVE" or leg.status != "ok" or not leg.duration_s:
                continue
            for journey in state["journeys"]:
                if journey.mode == "car" and names.get(leg.origin_id) == journey.origin and names.get(leg.destination_id) == journey.destination:
                    options.setdefault(journey.id, []).append(TransportOption(
                        id="road:" + journey.id, requested_mode="car", origin=journey.origin,
                        destination=journey.destination, departure_date=journey.departure_date,
                        duration_s=leg.duration_s, source_url="evidence:" + result.provider,
                        warnings=["Road guidance only; fuel, tolls, parking and timetable unknown."]))
    return options


def route_payload(state, entries):
    request = state["user_preferences"]
    end = request.preferences.end_location or request.destinations[-1]
    end = request.origin if end.casefold() == "origin" else end
    labels = list(dict.fromkeys([request.origin, *request.destinations, end]))
    from .identity import city_points
    locations = {label: {"id": label, "name": label,
                         "latitude": p.location.latitude, "longitude": p.location.longitude,
                         "timezone": "UTC", "country_code": "", "provider_reference": p.place_id}
                 for label, p in city_points(entries, labels).items()}
    offers = []
    for name, query, result in entries:
        data = result.data if isinstance(result.data, list) else [result.data]
        if name == "transport":
            for option in data:
                if not isinstance(option, TransportOption) or not option.duration_s:
                    continue
                matching = next((j for j in state["journeys"] if j.origin == option.origin
                    and j.destination == option.destination and j.mode == option.requested_mode
                    and j.departure_date == (query.departure_date or (query.departure_time.date() if query.departure_time else None))), None)
                if not matching:
                    continue
                priced = option.fare_amount is not None and option.fare_currency and option.fare_classification != "unknown"
                offers.append({"id": option.id, "origin_id": option.origin, "destination_id": option.destination,
                    "mode": option.requested_mode, "duration_s": option.duration_s,
                    "fare_amount": option.fare_amount if priced else None,
                    "currency": option.fare_currency if priced else None,
                    "fare_classification": option.fare_classification if priced else "unknown",
                    "fare_scope": option.fare_scope, "departure_date": str(matching.departure_date),
                    "source_url": option.source_url or "evidence:" + result.provider,
                    "basis": "Returned offer; UTC location placeholder used for pricing-only optimization, not scheduling."})
    if set(labels) - locations.keys():
        return None
    for items in road_options(state, entries).values():
        for o in items:
            offers.append({"id": o.id, "origin_id": o.origin, "destination_id": o.destination,
                           "mode": "car", "duration_s": o.duration_s, "source_url": o.source_url})
    modes = ["car" if str(m) == "self_driving" else str(m) for m in request.preferences.long_distance_modes or []]
    return {"start_location": locations[request.origin], "end_location": locations[end],
            "required_locations": [locations[x] for x in request.destinations if x not in (request.origin, end)],
            "start_date": str(request.start_date), "end_date": str(request.end_date),
            "adults": request.adults, "children": request.children, "budget": request.budget,
            "currency": request.currency or "INR", "allowed_modes": modes if modes and "any" not in modes else ["car", "train", "bus", "flight"],
            "fixed_order": True, "connections": list({o["id"]: o for o in offers}.values()), "fetch_road_data": False}


def local_agent_tools(role, state, entries, route_results=None):
    if state["run"].planning_profile == "full" or role not in ("itinerary_planning", "transport_booking", "accommodation", "response_aggregator"):
        return []

    async def calc(query):
        return calculate(query.model_dump(mode="json", exclude_none=True))

    tools = [AgentTool("calculate", "Local arithmetic. Use supplied values with matching units/currency; unknown is not zero.", CalculatorInput, calc, PlanningStage.ESTIMATING_COSTS)]
    if role == "transport_booking":
        async def route(query):
            payload = route_payload(state, entries)
            result = {"status": "unavailable", "warnings": ["Resolve every city including origin through Places before optimizing; no invented coordinates."]} if payload is None else await asyncio.to_thread(optimize_route, payload)
            result["scope"] = "Supplied eligible connections, fixed allocated city order; not all real-world services."
            result["evidence_provider"] = "synthetic_level1" if entries and all(r.provider == "synthetic_level1" for n, q, r in entries if n in ("places", "transport")) else "mixed_or_external"
            if route_results is not None:
                route_results.clear()
                route_results.update(result)
            return result
        tools.append(AgentTool("optimize_route", "Compare cheapest/balanced/fastest using searched city locations and returned transport offers. No arguments or network requests. Call after searches.", OptimizeInput, route, PlanningStage.CHECKING_ROUTES))
    return tools
