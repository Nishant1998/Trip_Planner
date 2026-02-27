"""Deterministic validation of generated itinerary output."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from itertools import permutations
from math import asin, cos, radians, sin, sqrt

from app.models.constraints import RelaxationStep
from app.models.evidence import EvidenceBundle, FactStatus
from app.models.context import PlanningContext
from app.models.plan import (
    PlanItem,
    PlanStatus,
    ValidationReport,
    TripPlan,
    ValidationIssue,
    ValidationResult,
    ValidationStatus,
)
from app.domain.meal_policy import meal_problem
from app.domain.opening_hours import (
    visit_is_within_opening_hours,
    visit_respects_last_entry,
)
from app.domain.route_policy import has_same_location_route

_ROAD_MODES = {"car", "taxi", "scooter", "driving", "bus", "coach"}
_DRIVING_WARNING_MINUTES = {
    "slow": 150,
    "relaxed": 180,
    "moderate": 300,
    "packed": 420,
}


def validate_generated_plan(
    plan: TripPlan,
    context: PlanningContext,
    evidence: EvidenceBundle,
) -> ValidationReport:
    """Validate hard constraints and score the usefulness of a materialized plan."""

    issues: list[ValidationIssue] = []
    valid_sources = evidence.source_ids()
    place_map = evidence.place_map()
    activity_items: list[PlanItem] = []
    evidence_activity_items: list[PlanItem] = []
    unsourced_activity_items: list[PlanItem] = []
    covered_stops: set[str] = set()

    starting_location = context.origin
    for day in plan.days:
        previous_end: datetime | None = None
        day_has_activity = False
        day_window_start = datetime.combine(day.date, context.day_start_time)
        day_window_end = datetime.combine(day.date, context.day_end_time)
        current_location = starting_location
        road_travel_minutes = 0

        for item in day.items:
            start, end = _item_datetimes(day.date, item)
            if end <= start:
                issues.append(
                    ValidationIssue(
                        code="INVALID_ITEM_DURATION",
                        severity="blocking",
                        message=f"{item.title!r} has a non-positive duration.",
                        day_number=day.day_number,
                        item_id=item.id,
                    )
                )

            is_pure_transfer_day = all(
                it.kind
                in {"transport", "check_in", "check_out", "meal", "rest", "free_time"}
                for it in day.items
            )
            is_travel_leg = bool(item.kind == "transport" and is_pure_transfer_day)
            is_arrival_checkin = bool(item.kind == "check_in" and is_pure_transfer_day)
            operational_extension = bool(
                is_travel_leg
                or is_arrival_checkin
                or (
                    item.kind in {"check_in", "check_out"}
                    and start >= day_window_start - timedelta(minutes=60)
                    and end <= day_window_end + timedelta(minutes=60)
                )
            )
            outside_window = (
                start < day_window_start or end > day_window_end
            ) and not operational_extension
            if outside_window:
                issues.append(
                    ValidationIssue(
                        code="OUTSIDE_DAY_WINDOW",
                        severity="blocking",
                        message=f"{item.title!r} falls outside the preferred day window.",
                        day_number=day.day_number,
                        item_id=item.id,
                    )
                )

            if (
                item.kind == "transport"
                and item.travel_from_previous is not None
                and (
                    (item.travel_from_previous.duration_minutes or 0) >= 24 * 60
                    or end - start >= timedelta(days=1)
                )
            ):
                issues.append(
                    ValidationIssue(
                        code="EXCESSIVE_TRAVEL_LEG",
                        severity="blocking",
                        message=(
                            f"The {item.travel_from_previous.mode} leg from "
                            f"{item.travel_from_previous.origin_label!r} to "
                            f"{item.travel_from_previous.destination_label!r} takes at "
                            "least 24 hours and needs a different mode or an overnight segment."
                        ),
                        day_number=day.day_number,
                        item_id=item.id,
                    )
                )

            if item.kind == "transport":
                leg = item.travel_from_previous
                if leg is None or leg.travel_window is None:
                    issues.append(
                        ValidationIssue(
                            code="TRAVEL_WINDOW_MISSING",
                            severity="blocking",
                            message=f"{item.title!r} has no structured travel window.",
                            day_number=day.day_number,
                            item_id=item.id,
                        )
                    )
                elif leg.status == FactStatus.UNAVAILABLE:
                    issues.append(
                        ValidationIssue(
                            code="ROUTE_UNAVAILABLE",
                            severity="blocking",
                            message=f"{item.title!r} has no usable route evidence.",
                            day_number=day.day_number,
                            item_id=item.id,
                        )
                    )
                elif round((end - start).total_seconds() / 60) != (
                    leg.travel_window.planned_minutes
                ):
                    issues.append(
                        ValidationIssue(
                            code="TRAVEL_BUFFER_MISMATCH",
                            severity="blocking",
                            message=f"{item.title!r} does not include its full travel buffer.",
                            day_number=day.day_number,
                            item_id=item.id,
                        )
                    )

                if leg is not None:
                    if leg.mode.casefold() in _ROAD_MODES:
                        road_travel_minutes += leg.duration_minutes or 0
                    if (
                        leg.origin_label.casefold() != current_location.casefold()
                        and not has_same_location_route(
                            evidence.routes,
                            current_location,
                            leg.origin_label,
                        )
                    ):
                        issues.append(
                            ValidationIssue(
                                code="ROUTE_CHAIN_BROKEN",
                                severity="blocking",
                                message=(
                                    f"{item.title!r} starts at {leg.origin_label!r}, but "
                                    f"the previous item leaves the traveller at "
                                    f"{current_location!r}."
                                ),
                                day_number=day.day_number,
                                item_id=item.id,
                            )
                        )
                    current_location = leg.destination_label

            if item.place is not None:
                if (
                    item.place.name.casefold() != current_location.casefold()
                    and not has_same_location_route(
                        evidence.routes,
                        current_location,
                        item.place.name,
                    )
                ):
                    issues.append(
                        ValidationIssue(
                            code="PLACE_ARRIVAL_MISSING",
                            severity="blocking",
                            message=(
                                f"{item.title!r} is scheduled at {item.place.name!r}, but "
                                f"the route leaves the traveller at {current_location!r}."
                            ),
                            day_number=day.day_number,
                            item_id=item.id,
                        )
                    )
                current_location = item.place.name

            if previous_end is not None and start < previous_end:
                issues.append(
                    ValidationIssue(
                        code="SCHEDULE_OVERLAP",
                        severity="blocking",
                        message=f"{item.title!r} overlaps the previous item.",
                        day_number=day.day_number,
                        item_id=item.id,
                    )
                )
            previous_end = max(previous_end or end, end)

            meal_issue = meal_problem(item.kind, item.title, start.time())
            if meal_issue:
                code, message = meal_issue
                issues.append(
                    ValidationIssue(
                        code=code,
                        severity="blocking",
                        message=message,
                        day_number=day.day_number,
                        item_id=item.id,
                    )
                )

            if item.kind == "activity":
                activity_items.append(item)
                day_has_activity = True
                if item.place and item.visit_duration is None:
                    issues.append(
                        ValidationIssue(
                            code="VISIT_DURATION_MISSING",
                            severity="blocking",
                            message=f"{item.title!r} has no structured visit-duration result.",
                            day_number=day.day_number,
                            item_id=item.id,
                        )
                    )
                if item.visit_duration and not (
                    item.visit_duration.minimum_minutes
                    <= item.visit_duration.scheduled_minutes
                    <= item.visit_duration.maximum_minutes
                ):
                    issues.append(
                        ValidationIssue(
                            code="VISIT_DURATION_OUTSIDE_RANGE",
                            severity="blocking",
                            message=f"{item.title!r} is scheduled outside its reasonable range.",
                            day_number=day.day_number,
                            item_id=item.id,
                        )
                    )
                if item.visit_duration:
                    expected_minutes = (
                        item.visit_duration.scheduled_minutes
                        + item.visit_duration.activity_buffer_minutes
                    )
                    actual_minutes = round((end - start).total_seconds() / 60)
                    if actual_minutes != expected_minutes:
                        issues.append(
                            ValidationIssue(
                                code="VISIT_OPERATIONAL_TIME_MISMATCH",
                                severity="blocking",
                                message=(
                                    f"{item.title!r} does not visibly include its complete "
                                    "access, waiting, visit, rest, and exit time."
                                ),
                                day_number=day.day_number,
                                item_id=item.id,
                            )
                        )
                if item.place and item.place.stop_name:
                    evidence_activity_items.append(item)
                    covered_stops.add(item.place.stop_name.casefold())
                else:
                    unsourced_activity_items.append(item)

            if item.place and item.place.id not in place_map:
                issues.append(
                    ValidationIssue(
                        code="UNKNOWN_PLACE_ID",
                        severity="blocking",
                        message=f"{item.place.id!r} was not present in gathered evidence.",
                        day_number=day.day_number,
                        item_id=item.id,
                    )
                )

            if item.place and item.place.id in place_map:
                place_evidence = place_map[item.place.id]
                if (
                    not place_evidence.provider_place_id
                    or not item.place.google_maps_url
                ):
                    issues.append(
                        ValidationIssue(
                            code="POI_IDENTITY_UNVERIFIED",
                            severity="blocking",
                            message=(
                                f"{item.place.name!r} has no exact provider place identity."
                            ),
                            day_number=day.day_number,
                            item_id=item.id,
                        )
                    )
                core_start, core_end = _activity_core_interval(item, start, end)
                hours_fit = visit_is_within_opening_hours(
                    place_evidence,
                    core_start,
                    core_end,
                )
                if hours_fit is None:
                    issues.append(
                        ValidationIssue(
                            code="OPENING_HOURS_UNAVAILABLE",
                            severity="warning",
                            message=(
                                f"Published opening periods are unavailable for "
                                f"{item.place.name!r}; confirm the proposed visit "
                                "time before travel."
                            ),
                            day_number=day.day_number,
                            item_id=item.id,
                        )
                    )
                elif not hours_fit:
                    issues.append(
                        ValidationIssue(
                            code="POI_CLOSED_DURING_VISIT",
                            severity="blocking",
                            message=(
                                f"{item.place.name!r} is scheduled outside its "
                                "published opening hours."
                            ),
                            day_number=day.day_number,
                            item_id=item.id,
                        )
                    )
                if item.visit_duration is not None and not visit_respects_last_entry(
                    place_evidence,
                    core_start,
                    item.visit_duration.last_entry_minutes_before_close,
                ):
                    issues.append(
                        ValidationIssue(
                            code="LAST_ENTRY_RULE_VIOLATED",
                            severity="blocking",
                            message=(
                                f"{item.place.name!r} starts after its source-backed "
                                "last-entry cutoff."
                            ),
                            day_number=day.day_number,
                            item_id=item.id,
                        )
                    )

            missing_sources = [
                source_id
                for source_id in item.source_ids
                if source_id not in valid_sources
            ]
            if missing_sources and _item_claims_verification(item):
                issues.append(
                    ValidationIssue(
                        code="MISSING_SOURCE",
                        severity="blocking",
                        message=f"Verified item references unknown sources: {missing_sources}.",
                        day_number=day.day_number,
                        item_id=item.id,
                    )
                )

        if not day_has_activity and not _is_expected_travel_only_day(context, day):
            issues.append(
                ValidationIssue(
                    code="EMPTY_ACTIVITY_DAY",
                    severity="warning",
                    message="This day has no destination activity.",
                    day_number=day.day_number,
                )
            )

        placed_items = [item for item in day.items if item.place is not None]
        is_last_day = day.day_number == len(plan.days)
        if placed_items:
            last_place_index = max(
                index for index, item in enumerate(day.items) if item.place is not None
            )
            later_destinations = {
                item.travel_from_previous.destination_label.casefold()
                for item in day.items[last_place_index + 1 :]
                if item.travel_from_previous is not None
            }
            # On the last day the traveller normally heads back to
            # ``context.end_location`` rather than the day base.  Accept either
            # target so the RETURN_TO_BASE check does not fire during the trip's
            # closing leg.
            valid_targets = {day.base_stop.casefold()}
            if is_last_day and context.end_location:
                valid_targets.add(context.end_location.casefold())
            last_place = placed_items[-1].place
            co_located_with_base = bool(
                last_place
                and has_same_location_route(
                    evidence.routes,
                    last_place.name,
                    day.base_stop,
                )
            )
            if not (valid_targets & later_destinations) and not co_located_with_base:
                # If the omission is already disclosed (partial-coverage mode
                # dropped the closing return leg), downgrade to a warning.
                already_disclosed = is_last_day and any(
                    r.code in {"RETURN_LEG_OMITTED"}
                    or (
                        r.step == RelaxationStep.OMIT_UNFITTABLE_STOP
                        and context.end_location
                        and context.end_location.casefold()
                        in {aid.casefold() for aid in r.affected_ids}
                    )
                    for r in plan.relaxations
                )
                issues.append(
                    ValidationIssue(
                        code="RETURN_TO_BASE_MISSING",
                        severity="warning" if already_disclosed else "blocking",
                        message=f"Day {day.day_number} does not return to {day.base_stop!r}.",
                        day_number=day.day_number,
                    )
                )

        driving_issue = _heavy_driving_issue(
            day.day_number,
            road_travel_minutes,
            context,
        )
        if driving_issue is not None:
            issues.append(driving_issue)

        starting_location = day.base_stop

    if not evidence.places:
        issues.append(
            ValidationIssue(
                code="PLACE_EVIDENCE_UNAVAILABLE",
                severity="blocking",
                message=(
                    "No place evidence was available, so a useful itinerary could not be built."
                ),
            )
        )

    if not evidence_activity_items:
        issues.append(
            ValidationIssue(
                code="NO_MEANINGFUL_ACTIVITIES",
                severity="blocking",
                message="The itinerary contains no evidence-backed destination activities.",
            )
        )

    if unsourced_activity_items:
        issues.append(
            ValidationIssue(
                code="UNSOURCED_ACTIVITIES",
                severity="blocking",
                message=(
                    f"{len(unsourced_activity_items)} activity item(s) are not linked "
                    "to place evidence. Use free_time for generic suggestions."
                ),
            )
        )

    missing_stops = [
        stop for stop in plan.resolved_stops if stop.casefold() not in covered_stops
    ]
    if missing_stops:
        # If the traveller was already told (via an ``OMIT_UNFITTABLE_STOP``
        # relaxation) that these stops were dropped for feasibility we
        # downgrade the check to a warning so a partial-but-honest plan can
        # still be delivered.
        already_disclosed = {
            aid.casefold()
            for r in plan.relaxations
            if r.step == RelaxationStep.OMIT_UNFITTABLE_STOP
            for aid in r.affected_ids
        }
        undisclosed_missing = [
            stop for stop in missing_stops if stop.casefold() not in already_disclosed
        ]
        missing_ratio = len(missing_stops) / max(1, len(plan.resolved_stops))
        if undisclosed_missing:
            severity = (
                "blocking"
                if missing_ratio >= 0.5 or _requires_complete_stop_coverage(context)
                else "warning"
            )
        else:
            severity = "warning"
        issues.append(
            ValidationIssue(
                code="DESTINATION_COVERAGE_INCOMPLETE",
                severity=severity,
                message=(
                    "No activity was scheduled at the following resolved stop(s): "
                    + ", ".join(missing_stops)
                    + "."
                ),
            )
        )

    minimum_exceeds_budget = (
        plan.budget.estimated_minimum
        > plan.budget.total_budget - plan.budget.contingency_reserved
    )
    maximum_exceeds_total_budget = (
        plan.budget.estimated_maximum > plan.budget.total_budget
    )
    if not plan.budget.within_budget:
        issues.append(
            ValidationIssue(
                code=(
                    "BUDGET_EXCEEDED" if minimum_exceeds_budget else "BUDGET_RANGE_RISK"
                ),
                severity="blocking" if minimum_exceeds_budget else "warning",
                message=(
                    "Even the minimum estimate exceeds the spendable budget."
                    if minimum_exceeds_budget
                    else (
                        "The conservative upper estimate exceeds the spendable budget; "
                        "use lower-cost options and confirm prices before booking."
                    )
                ),
            )
        )
    elif maximum_exceeds_total_budget:
        issues.append(
            ValidationIssue(
                code="BUDGET_UPPER_RANGE_OVER_TOTAL",
                severity="warning",
                message=(
                    "The conservative upper estimate exceeds the total trip budget; "
                    "keep bookings and discretionary spending below the budget ceiling."
                ),
            )
        )

    unavailable_costs = [
        category
        for category, cost in plan.budget.category_totals.items()
        if cost.status == FactStatus.UNAVAILABLE
    ]
    if unavailable_costs:
        issues.append(
            ValidationIssue(
                code="COST_CONVERSION_UNAVAILABLE",
                severity="warning",
                message=(
                    "Some local-currency estimates were excluded because conversion "
                    f"was unavailable: {', '.join(unavailable_costs)}."
                ),
            )
        )

    if context.end_location and plan.days:
        last_transport = next(
            (
                item.travel_from_previous
                for item in reversed(plan.days[-1].items)
                if item.travel_from_previous is not None
            ),
            None,
        )
        if (
            last_transport is None
            or last_transport.destination_label.casefold()
            != context.end_location.casefold()
        ):
            # If the traveller was already told this return leg was omitted for
            # feasibility we downgrade to a warning so a partial plan can still
            # be delivered.  A completely undisclosed missing return remains a
            # blocking bug.
            return_disclosed = any(
                r.code == "RETURN_LEG_OMITTED"
                or (
                    r.step == RelaxationStep.OMIT_UNFITTABLE_STOP
                    and context.end_location.casefold()
                    in {aid.casefold() for aid in r.affected_ids}
                )
                for r in plan.relaxations
            )
            issues.append(
                ValidationIssue(
                    code="RETURN_TRAVEL_MISSING",
                    severity="warning" if return_disclosed else "blocking",
                    message=f"The itinerary does not end at {context.end_location!r}.",
                )
            )

    searchable_text = _searchable_plan_text(plan, evidence)
    for must_see in context.must_see:
        if must_see.casefold() not in searchable_text:
            issues.append(
                ValidationIssue(
                    code="MUST_SEE_MISSING",
                    severity="blocking",
                    message=f"Required place {must_see!r} is absent from the itinerary.",
                )
            )

    for avoided in context.avoid:
        if avoided.casefold() in searchable_text:
            issues.append(
                ValidationIssue(
                    code="AVOID_CONFLICT",
                    severity="blocking",
                    message=f"Avoided preference {avoided!r} appears in the itinerary.",
                )
            )

    missing_notes = [
        note
        for note in context.trip_context_notes
        if note not in plan.context_notes_applied
    ]
    if missing_notes:
        issues.append(
            ValidationIssue(
                code="CONTEXT_NOT_ACKNOWLEDGED",
                severity="warning",
                message=f"{len(missing_notes)} trip context note(s) were not acknowledged.",
            )
        )

    if _requires_rest(context) and not _contains_kind(plan, {"rest", "free_time"}):
        issues.append(
            ValidationIssue(
                code="REST_REQUIREMENT_NOT_APPLIED",
                severity="blocking",
                message=(
                    "The itinerary does not include rest time required by the traveller profile."
                ),
            )
        )

    if _notes_contain(
        context, {"flexible evening", "relaxed evening"}
    ) and not _contains_kind(plan, {"free_time"}):
        issues.append(
            ValidationIssue(
                code="FLEXIBLE_TIME_NOT_APPLIED",
                severity="warning",
                message="The requested flexible evening is not visible in the itinerary.",
            )
        )

    if _notes_contain(context, {"shopping"}) and "shopping" not in searchable_text:
        issues.append(
            ValidationIssue(
                code="SHOPPING_CONTEXT_NOT_APPLIED",
                severity="warning",
                message="The requested shopping time is not visible in the itinerary.",
            )
        )

    if context.dietary_restrictions and not any(
        restriction.casefold() in searchable_text
        for restriction in context.dietary_restrictions
    ):
        issues.append(
            ValidationIssue(
                code="DIETARY_REQUIREMENT_NOT_APPLIED",
                severity="warning",
                message="The meal plan does not visibly apply the dietary requirements.",
            )
        )

    if context.activity_interests and not any(
        _interest_is_represented(interest, searchable_text)
        for interest in context.activity_interests
    ):
        issues.append(
            ValidationIssue(
                code="INTERESTS_NOT_REPRESENTED",
                severity="warning",
                message="The itinerary does not visibly represent the requested interests.",
            )
        )

    route_efficiency_issue = _route_efficiency_issue(plan)
    if route_efficiency_issue is not None:
        issues.append(route_efficiency_issue)

    issues = _dedupe_issues(issues)
    blocking = [issue for issue in issues if issue.severity == "blocking"]
    warnings = [issue for issue in issues if issue.severity == "warning"]

    return ValidationReport(
        hard_constraints_passed=not blocking,
        issues=[*blocking, *warnings],
    )


def apply_validation_result(plan: TripPlan, report: ValidationReport) -> TripPlan:
    updated = plan.model_copy(deep=True)
    updated.blocking_issues = [
        issue for issue in report.issues if issue.severity == "blocking"
    ]
    warnings = [issue for issue in report.issues if issue.severity == "warning"]
    updated.validation = ValidationResult(
        status=(
            ValidationStatus.REJECTED
            if updated.blocking_issues
            else ValidationStatus.APPROVED_WITH_WARNINGS
            if warnings
            else ValidationStatus.APPROVED
        ),
        hard_constraints_passed=report.hard_constraints_passed,
        warnings=warnings,
        blocking_issues=updated.blocking_issues,
    )
    if updated.blocking_issues:
        updated.status = PlanStatus.INFEASIBLE
    elif updated.status != PlanStatus.PARTIAL:
        updated.status = PlanStatus.COMPLETE
    updated.warnings = _dedupe_strings(
        [
            *updated.warnings,
            *(issue.message for issue in warnings),
        ]
    )
    return updated


def _item_datetimes(day_date: date, item: PlanItem) -> tuple[datetime, datetime]:
    start = item.start_datetime or datetime.combine(day_date, item.start_time)
    end = item.end_datetime or datetime.combine(day_date, item.end_time)
    if item.end_datetime is None and item.end_time <= item.start_time:
        end += timedelta(days=1)
    return start, end


def _activity_core_interval(
    item: PlanItem,
    start: datetime,
    end: datetime,
) -> tuple[datetime, datetime]:
    """Separate the opening-hours visit from access, queue, rest, and exit time."""

    duration = item.visit_duration
    if duration is None:
        return start, end
    core_start = start + timedelta(
        minutes=duration.access_minutes + duration.waiting_minutes
    )
    core_end = core_start + timedelta(minutes=duration.scheduled_minutes)
    return core_start, min(core_end, end)


def _item_claims_verification(item: PlanItem) -> bool:
    return bool(
        (item.place and item.place.status == FactStatus.VERIFIED)
        or (
            item.travel_from_previous
            and item.travel_from_previous.status == FactStatus.VERIFIED
        )
    )


def _is_expected_travel_only_day(context: PlanningContext, day: object) -> bool:
    """Allow a true arrival/departure day, never an empty sightseeing day."""

    items = getattr(day, "items", [])
    if not items:
        return False
    transport_minutes = sum(
        item.travel_from_previous.travel_window.planned_minutes
        for item in items
        if item.kind == "transport"
        and item.travel_from_previous is not None
        and item.travel_from_previous.travel_window is not None
    )
    # Three hours is long enough that an arrival/departure day can reasonably
    # contain only settling, meals, and travel. A short flexible day is not.
    return transport_minutes >= 180


def _requires_complete_stop_coverage(context: PlanningContext) -> bool:
    return any(
        phrase in item.casefold()
        for item in context.destination.items
        for phrase in ("ashtavinayak", "circuit", "pilgrimage")
    )


def _requires_rest(context: PlanningContext) -> bool:
    return bool(
        context.num_seniors
        or context.accessibility_needs
        or _notes_contain_any_word(context, {"rest", "unhurried", "queue"})
    )


def _heavy_driving_issue(
    day_number: int,
    road_travel_minutes: int,
    context: PlanningContext,
) -> ValidationIssue | None:
    style = context.travel_style.casefold()
    threshold = _DRIVING_WARNING_MINUTES.get(
        style, _DRIVING_WARNING_MINUTES["moderate"]
    )
    if context.num_seniors and threshold > 180:
        threshold -= 60
    if road_travel_minutes <= threshold:
        return None
    traveller_note = " with a senior traveller" if context.num_seniors else ""
    return ValidationIssue(
        code="HEAVY_DRIVING_DAY",
        severity="warning",
        message=(
            f"Day {day_number} includes approximately "
            f"{road_travel_minutes / 60:.1f} hours of routed road travel"
            f"{traveller_note}, above the {style or 'moderate'}-style comfort "
            f"threshold of {threshold / 60:.1f} hours. Plan an additional "
            "roadside rest or chai stop."
        ),
        day_number=day_number,
    )


def _route_efficiency_issue(plan: TripPlan) -> ValidationIssue | None:
    """Catch severe geographic backtracking as a final independent safety gate."""

    ordered: list[tuple[str, float, float]] = []
    for day in plan.days:
        for item in day.items:
            place = item.place
            if (
                place is None
                or not place.stop_name
                or place.latitude is None
                or place.longitude is None
            ):
                continue
            key = place.stop_name.casefold()
            if ordered and ordered[-1][0] == key:
                continue
            if any(existing[0] == key for existing in ordered):
                # Revisiting a stop is itself represented in the actual distance, but
                # the best comparison operates on the same unique stop set.
                continue
            ordered.append((key, place.latitude, place.longitude))
    if len(ordered) < 4 or len(ordered) > 8:
        return None

    actual = _coordinate_path_distance(ordered)
    best = min(
        _coordinate_path_distance([ordered[index] for index in order])
        for order in permutations(range(len(ordered)))
    )
    excess = actual - best
    ratio = actual / best if best else 1.0
    if ratio >= 1.55 and excess >= 100:
        return ValidationIssue(
            code="ROUTE_GROSSLY_INEFFICIENT",
            severity="blocking",
            message=(
                "The stop sequence has severe geographic backtracking: its straight-line "
                f"path is about {ratio:.2f}× the best ordering for the same stops. "
                "The plan must be rebuilt from a provider road-time matrix."
            ),
        )
    if ratio >= 1.30 and excess >= 60:
        return ValidationIssue(
            code="ROUTE_BACKTRACKING_WARNING",
            severity="warning",
            message=(
                "The stop sequence appears to backtrack geographically. Recheck the "
                "provider road-time matrix before booking transport."
            ),
        )
    return None


def _coordinate_path_distance(points: list[tuple[str, float, float]]) -> float:
    return sum(
        _haversine_km(origin[1], origin[2], destination[1], destination[2])
        for origin, destination in zip(points, points[1:], strict=False)
    )


def _haversine_km(
    origin_latitude: float,
    origin_longitude: float,
    destination_latitude: float,
    destination_longitude: float,
) -> float:
    lat1 = radians(origin_latitude)
    lat2 = radians(destination_latitude)
    delta_lat = lat2 - lat1
    delta_lon = radians(destination_longitude - origin_longitude)
    value = sin(delta_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(delta_lon / 2) ** 2
    return 2 * 6371 * asin(sqrt(value))


def _notes_contain(context: PlanningContext, phrases: set[str]) -> bool:
    return any(
        phrase in note.casefold()
        for note in context.trip_context_notes
        for phrase in phrases
    )


def _notes_contain_any_word(context: PlanningContext, words: set[str]) -> bool:
    """Word-boundary match so ``rest`` does not accidentally match ``restaurant``."""

    pattern = re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b")
    return any(
        pattern.search(note.casefold()) is not None
        for note in context.trip_context_notes
    )


def _contains_kind(plan: TripPlan, kinds: set[str]) -> bool:
    return any(item.kind in kinds for day in plan.days for item in day.items)


def _searchable_plan_text(plan: TripPlan, evidence: EvidenceBundle) -> str:
    item_text = " ".join(
        f"{item.title} {item.description} {item.place.name if item.place else ''}"
        for day in plan.days
        for item in day.items
    )
    category_text = " ".join(
        category for place in evidence.places for category in place.categories
    )
    return f"{plan.title} {plan.summary} {item_text} {category_text}".casefold()


def _interest_is_represented(interest: str, searchable_text: str) -> bool:
    normalized = interest.casefold().strip()
    if not normalized:
        return True
    variants = {normalized, normalized.rstrip("s"), f"{normalized}s"}
    return any(variant and variant in searchable_text for variant in variants)


def _dedupe_issues(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    result: list[ValidationIssue] = []
    seen: set[tuple[str, int | None, str | None, str]] = set()
    for issue in issues:
        key = (issue.code, issue.day_number, issue.item_id, issue.message)
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = _warning_key(value)
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _warning_key(value: str) -> str:
    """Deduplicate equivalent traveller warnings emitted by different stages."""

    normalized = value.strip().casefold()
    opening_hours = re.search(
        r"published opening periods are unavailable for ['\"]?(?P<place>.+?)['\"]?;",
        normalized,
    )
    if opening_hours:
        place = opening_hours.group("place").strip(" '\"")
        return f"opening-hours-unavailable:{place}"
    return normalized
