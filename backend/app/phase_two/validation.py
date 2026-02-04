"""Deterministic feasibility checks; model opinions cannot override these."""

from datetime import datetime, time, timedelta

from .contracts import CostRecord, ValidationIssue
from .daywise import arithmetic, daywise_issues


def within_regular_hours(hours, start, end):
    """True/False for usable weekly periods, None for unparseable/unknown data."""
    periods = hours.get("periods") if isinstance(hours, dict) else None
    if not isinstance(periods, list) or not start or not end:
        return None
    if not periods:
        return None
    week = 7 * 24 * 60
    begins = ((start.weekday() + 1) % 7) * 1440 + start.hour * 60 + start.minute
    finishes = begins + (end - start).total_seconds() / 60
    usable = False
    for period in periods:
        try:
            opened, closed = period["open"], period.get("close")
            left = (
                opened["day"] * 1440
                + opened.get("hour", 0) * 60
                + opened.get("minute", 0)
            )
            if closed is None:
                if left == 0:
                    return True  # Google's 24/7 representation.
                continue
            right = (
                closed["day"] * 1440
                + closed.get("hour", 0) * 60
                + closed.get("minute", 0)
            )
            if right <= left:
                right += week
            usable = True
            if any(
                left <= begins + shift and finishes + shift <= right
                for shift in (-week, 0, week)
            ):
                return True
        except (KeyError, TypeError, ValueError):
            continue
    return False if usable else None


def validate_plan(state):
    request = state["user_preferences"]
    prefs = request.preferences
    issues, totals, unknown = [], {}, []

    def issue(code, message, affected=None, owner="schedule", severity="error"):
        issues.append(
            ValidationIssue(
                code=code,
                message=message,
                affected_id=affected,
                owner=owner,
                severity=severity,
            )
        )

    def add(category, amount, currency, verified_scope):
        if (
            amount is None
            or not currency
            or currency != request.currency
            or not verified_scope
        ):
            unknown.append(category)
        else:
            totals[category] = arithmetic("a + b", a=totals.get(category, 0), b=amount)

    expected = (request.end_date - request.start_date).days + 1
    days = state["itinerary"]
    if [d.date for d in days] != [
        request.start_date + timedelta(days=n) for n in range(expected)
    ]:
        issue("date_coverage", "Every requested calendar day must be represented.")
    places = {p.id: p for p in state["places"]}
    routes = {(r.origin_id, r.destination_id, r.mode.upper()): r
              for r in state["route_matrix"]}
    requested_modes = {
        (r.origin_id, r.destination_id): {
            "driving": "DRIVE", "walking": "WALK", "bicycling": "BICYCLE",
            "transit": "TRANSIT",
        }[r.mode]
        for r in state["route_requirements"]
    }
    selected_journeys = {t.id: t.selected for t in state["transport_options"]
                         if t.selected}
    allowed_long_modes = {"car" if str(mode) == "self_driving" else str(mode) for mode in prefs.long_distance_modes or []}
    allowed_local_modes = {str(mode) for mode in prefs.local_transport_modes or []}
    local_route_modes = {
        "walking": "walking", "taxi": "driving", "self_driving": "driving",
        "public_transport": "transit",
    }
    if allowed_local_modes and "any" not in allowed_local_modes:
        allowed_routes = {local_route_modes[mode] for mode in allowed_local_modes
                          if mode in local_route_modes}
        for requirement in state["route_requirements"]:
            if requirement.mode not in allowed_routes:
                issue("local_mode_conflict",
                      "A requested local route uses a disallowed travel mode.",
                      f"{requirement.origin_id}->{requirement.destination_id}",
                      "schedule")
    visited_destinations = set()
    names, ids = [], set()
    for day_index, day in enumerate(days):
        previous_end, previous_place = None, None
        if day.base_location:
            visited_destinations.add(day.base_location.casefold())
        if day.base_location and day.base_location not in request.destinations:
            issue(
                "destination_unrequested",
                "Base location is outside the confirmed destination list.",
                str(day.day),
                "allocation",
            )
        if day_index and day.base_location and days[day_index - 1].base_location:
            previous_base = days[day_index - 1].base_location
            if previous_base.casefold() != day.base_location.casefold() and not any(
                j.origin.casefold() == previous_base.casefold()
                and j.destination.casefold() == day.base_location.casefold()
                and j.departure_date in (days[day_index - 1].date, day.date)
                for j in state["journeys"]
            ):
                issue("transfer_missing", "A change of base needs an allocated journey.",
                      str(day.day), "allocation")
        if not day.events or all(e.kind == "unallocated" for e in day.events):
            issue(
                "day_unallocated", "This day has no supported schedule.", str(day.day)
            )
        activity_count = sum(e.kind == "activity" for e in day.events)
        if activity_count and not any(e.kind == "meal" for e in day.events):
            issue("meal_missing", "Include a meal break.", str(day.day))
        if activity_count >= 3 and not any(e.kind == "rest" for e in day.events):
            issue("rest_missing", "Include a rest break.", str(day.day))
        if str(prefs.travel_style) in ("relaxed", "slow") and activity_count > 3:
            issue(
                "pace",
                "Reduce activities for the requested relaxed pace.",
                str(day.day),
            )
        for event in day.events:
            if event.id in ids:
                issue("duplicate_event", "Event IDs must be unique.", event.id)
            ids.add(event.id)
            names.append(event.title.casefold())
            if event.visit_minutes is not None and event.duration_basis == "unknown":
                issue("duration_basis_unknown",
                      "Visit duration needs a source or a labeled planning allowance.",
                      event.id, severity="warning")
            if event.visit_minutes is not None and event.duration_basis == "provider":
                issue("duration_provider_unverified",
                      "No provider visit-duration field is stored for this event.",
                      event.id)
            if event.place_id and event.place_id not in places:
                issue(
                    "unsupported_place",
                    "Place has no research evidence.",
                    event.id,
                    "research",
                )
            if any(term.casefold() in event.title.casefold() for term in request.avoid):
                issue(
                    "excluded_activity",
                    "This event conflicts with an explicit exclusion.",
                    event.id,
                )
            if event.start and event.end:
                place = places.get(event.place_id) if event.place_id else None
                destination = next((d.label for d in state["destinations"]
                                    if place and d.id == place.destination_id),
                                   day.base_location)
                for journey in state["journeys"]:
                    selected = selected_journeys.get(journey.id)
                    arrival = selected.arrival if selected else None
                    if (destination and journey.destination.casefold() == destination.casefold()
                            and arrival and arrival.date() == day.date
                            and arrival.utcoffset() is not None
                            and event.start.utcoffset() is not None
                            and event.kind == "activity" and event.start < arrival):
                        issue("activity_before_arrival",
                              "An activity starts before the selected journey arrives.",
                              event.id)
                if event.start.utcoffset() is None or event.end.utcoffset() is None:
                    issue(
                        "timezone_unknown",
                        "Times need explicit time-zone offsets before verification.",
                        event.id,
                    )
                elif (
                    previous_end
                    and previous_end.utcoffset() is not None
                    and event.start < previous_end
                ):
                    issue(
                        "event_overlap",
                        "Event starts before the previous event ends.",
                        event.id,
                    )
                if event.start.date() != day.date:
                    issue(
                        "event_date",
                        "Event start does not match its assigned local day.",
                        event.id,
                    )
                if prefs.day_start_time and event.start.time() < prefs.day_start_time:
                    issue(
                        "day_window",
                        "Event starts before the requested daily window.",
                        event.id,
                    )
                if (
                    prefs.day_end_time
                    and event.end.time() > prefs.day_end_time
                    and event.kind != "travel"
                ):
                    issue(
                        "day_window",
                        "Event ends after the requested daily window.",
                        event.id,
                    )
                if (
                    event.place_id
                    and previous_place
                    and previous_place != event.place_id
                ):
                    mode = requested_modes.get((previous_place, event.place_id), "DRIVE")
                    route = routes.get((previous_place, event.place_id, mode))
                    if (
                        route
                        and route.duration_s is not None
                        and previous_end
                        and previous_end.utcoffset() is not None
                        and event.start.utcoffset() is not None
                        and event.start
                        < previous_end + timedelta(seconds=route.duration_s)
                    ):
                        issue(
                            "travel_gap",
                            "Insufficient time between visits for the sourced journey.",
                            event.id,
                        )
                previous_end = event.end
            elif event.kind not in ("unallocated",):
                issue(
                    "timing_unknown",
                    "Event cannot be verified without start/end times.",
                    event.id,
                )
            if event.place_id:
                if previous_place and previous_place != event.place_id:
                    mode = requested_modes.get((previous_place, event.place_id), "DRIVE")
                    route = routes.get((previous_place, event.place_id, mode))
                    if (
                        not route
                        or route.duration_s is None
                        or route.distance_m is None
                        or route.status != "ok"
                    ):
                        issue(
                            "route_missing",
                            "Directed travel time/distance is missing.",
                            event.id,
                            "transport",
                        )
                previous_place = event.place_id
                place = places.get(event.place_id)
                if place:
                    label = next((d.label for d in state["destinations"]
                                  if d.id == place.destination_id), None)
                    if label:
                        visited_destinations.add(label.casefold())
                if place and event.kind == "activity":
                    if not place.opening_hours:
                        issue(
                            "hours_unknown",
                            "Opening hours are unknown; visit remains tentative.",
                            event.id,
                            "research",
                        )
                    else:
                        fits = within_regular_hours(
                            place.opening_hours, event.start, event.end
                        )
                        if fits is False:
                            issue(
                                "outside_opening_hours",
                                "Visit falls outside supplied regular opening periods.",
                                event.id,
                            )
                        elif fits is None:
                            issue(
                                "hours_unparseable",
                                "Usable opening periods are unavailable; do not claim the visit is confirmed.",
                                event.id,
                                severity="warning",
                            )
                        issue(
                            "hours_tentative",
                            "Regular hours do not confirm holiday or one-off availability.",
                            event.id,
                            severity="warning",
                        )
            if event.kind in ("activity", "meal", "travel"):
                add(
                    "activities_and_local_travel",
                    event.estimated_cost,
                    event.currency,
                    False,
                )
    for destination in request.destinations:
        if destination.casefold() not in visited_destinations:
            issue("destination_missing",
                  "A requested destination has no planned day or visit.",
                  destination, "allocation")
    if prefs.end_location:
        endpoint = (request.origin if prefs.end_location.casefold() == "origin"
                    else prefs.end_location)
        reached = bool(days and days[-1].base_location
                       and days[-1].base_location.casefold() == endpoint.casefold()) or any(
            journey.destination.casefold() == endpoint.casefold()
            and selected_journeys.get(journey.id) is not None
            and selected_journeys[journey.id].arrival is not None
            and selected_journeys[journey.id].arrival.date() <= request.end_date
            for journey in state["journeys"]
        )
        if not reached:
            issue("endpoint_unverified",
                  "The required final location is absent from the final base and selected arrivals.",
                  endpoint, "allocation")
    for must in request.must_see:
        if not any(must.casefold() in name for name in names):
            issue(
                "must_see_unresolved",
                f"Requested must-see remains unresolved: {must}",
                must,
            )
    if prefs.dietary_restrictions:
        issue(
            "dietary_unverified",
            "Dietary suitability requires explicit provider confirmation; not inferred from restaurant type.",
            severity="warning",
        )
    if request.accessibility_needs:
        issue(
            "accessibility_unverified",
            "Accessibility needs remain unverified unless explicit supporting evidence is supplied.",
            severity="warning",
        )
    if any(
        (prefs.depart_time_window, prefs.arrival_time_window, prefs.return_time_window)
    ):
        issue(
            "travel_window_unverified",
            "Verify requested travel windows against the selected zoned journey times.",
            owner="transport",
        )
    stays = state["stay_requests"]
    for stay in stays:
        if not any(d.id == stay.destination_id for d in state["destinations"]):
            issue("stay_destination_unknown",
                  "Stay destination has no researched destination record.",
                  stay.id, "allocation")
        if stay.adults != request.adults or stay.children != request.children:
            issue(
                "stay_party",
                "Stay party differs from the confirmed travelers.",
                stay.id,
                "allocation",
            )
        if stay.check_in < request.start_date or stay.check_out > request.end_date:
            issue(
                "stay_dates",
                "Stay falls outside requested nights.",
                stay.id,
                "allocation",
            )
        hotel = next((h for h in state["hotels"] if h.stay_request.id == stay.id), None)
        selected = hotel.selected if hotel else None
        if selected:
            multiplier = (
                (stay.check_out - stay.check_in).days
                if selected.price_basis == "per_night"
                else 1
            )
            add(
                "accommodation",
                arithmetic("price * nights", price=selected.price, nights=multiplier) if selected.price is not None else None,
                selected.currency,
                selected.price_basis in ("total", "per_night"),
            )
        else:
            unknown.append("accommodation")
            issue(
                "stay_missing",
                "No supported hotel selected for this stay.",
                stay.id,
                "accommodation",
            )
    for n in range(expected - 1):
        night = request.start_date + timedelta(days=n)
        stay_count = sum(s.check_in <= night < s.check_out for s in stays)
        overnight_count = 0
        for selected in selected_journeys.values():
            departure, arrival = selected.departure, selected.arrival
            if (departure and arrival and departure.utcoffset() is not None
                    and arrival.utcoffset() is not None):
                midnight = datetime.combine(night + timedelta(days=1), time.min,
                                            tzinfo=departure.tzinfo)
                if departure < midnight <= arrival:
                    overnight_count += 1
        if stay_count + overnight_count != 1:
            issue(
                "night_coverage",
                "Night needs exactly one stay or selected overnight journey.",
                str(night),
                "allocation",
            )
            unknown.append("accommodation")
    for journey in state.get("journeys", []):
        if (allowed_long_modes and "any" not in allowed_long_modes
                and journey.mode not in allowed_long_modes):
            issue("journey_mode_conflict",
                  "The allocated journey uses a disallowed travel mode.",
                  journey.id, "allocation")
        selected = next(
            (t.selected for t in state["transport_options"] if t.id == journey.id), None
        )
        if not selected:
            issue(
                "journey_missing",
                "Requested journey has no supported option.",
                journey.id,
                "transport",
            )
            unknown.append("transport")
        else:
            add(
                "transport",
                selected.fare_amount,
                selected.fare_currency,
                selected.fare_scope == "passenger_group"
                and selected.fare_classification == "quoted",
            )
            if not selected.departure or not selected.arrival:
                issue(
                    "journey_time_unknown",
                    "Selected journey lacks verified zoned departure/arrival times.",
                    journey.id,
                    "transport",
                )
    if not state.get("journeys"):
        unknown.append("transport")
    if not days:
        unknown.append("activities_and_local_travel")
    known = arithmetic("round(sum(values), 2)", values=list(totals.values()) or [0])
    budget = (
        "over"
        if request.budget is not None and known > request.budget
        else "within"
        if not unknown and request.budget is not None
        else "unknown"
    )
    if state["run"].planning_profile != "full":
        issues = daywise_issues(state, issues)
        unknown = [item for item in unknown if item != "activities_and_local_travel"]
        unknown.extend(["admissions", "meals", "local_transport"])
    return issues, CostRecord(
        currency=request.currency,
        known_subtotal=known,
        category_totals=totals,
        unknown_components=sorted(set(unknown)),
        budget_status=budget,
    )
