"""Deterministic compiler from an approved plan to display-ready trip content."""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from urllib.parse import quote_plus

from app.models.evidence import EvidenceBundle, PlaceEvidence
from app.models.context import PlanningContext
from app.models.presentation import (
    PresentationAlert,
    PresentationBudget,
    PresentationDay,
    PresentationFact,
    PresentationStay,
    PresentationTimelineItem,
    TripPresentation,
)
from app.models.plan import MoneyRange, PlanItem, TripPlan
from app.domain.route_policy import SAME_LOCATION_DISTANCE_KM

_ITEM_ICONS = {
    "transport": "🚗",
    "activity": "🛕",
    "meal": "🍽️",
    "rest": "🛌",
    "free_time": "🕊️",
}
_ACTION_WARNING_CODES = {
    "OPENING_HOURS_UNAVAILABLE",
    "HEAVY_DRIVING_DAY",
    "BUDGET_UPPER_RANGE_OVER_TOTAL",
    "BUDGET_RANGE_RISK",
}


class CompilerEngine:
    """Render an approved plan without changing its decisions or calling providers."""

    def compile(
        self,
        plan: TripPlan,
        context: PlanningContext,
        evidence: EvidenceBundle,
    ) -> TripPresentation:
        alerts = _alerts(plan)
        budget = _budget(plan)
        days = [
            PresentationDay(
                day_number=day.day_number,
                date_label=day.date.strftime("%a, %d %b %Y"),
                base_stop=day.base_stop,
                theme=day.theme,
                daily_cost=_money_range(day.daily_cost),
                alerts=[
                    alert for alert in alerts if alert.day_number == day.day_number
                ],
                timeline=[
                    item
                    for source_item in day.items
                    if (item := _timeline_item(source_item, context, evidence))
                    is not None
                ],
            )
            for day in plan.days
        ]
        stays = [
            PresentationStay(
                stop=stay.stop,
                nights=stay.nights,
                description=stay.description,
                estimated_cost=_money_range(stay.estimated_cost),
            )
            for stay in plan.stays
        ]
        quick_facts = [
            PresentationFact(
                label="Dates",
                value=(
                    f"{plan.start_date.strftime('%d %b %Y')} – "
                    f"{plan.end_date.strftime('%d %b %Y')}"
                ),
            ),
            PresentationFact(label="Duration", value=f"{len(plan.days)} days"),
            PresentationFact(label="Travellers", value=_traveller_label(context)),
            PresentationFact(
                label="Travel style", value=context.travel_style or "Not specified"
            ),
        ]
        validation_status = (
            plan.validation.status.value.replace("_", " ").title()
            if plan.validation is not None
            else "Not available"
        )
        checklist = _checklist(plan, alerts)
        presentation = TripPresentation(
            title=plan.title,
            summary=plan.summary,
            quick_facts=quick_facts,
            validation_status=validation_status,
            alerts=alerts,
            budget=budget,
            days=days,
            stays=stays,
            checklist=checklist,
            markdown="",
        )
        return presentation.model_copy(update={"markdown": _markdown(presentation)})


def _timeline_item(
    item: PlanItem,
    context: PlanningContext,
    evidence: EvidenceBundle,
) -> PresentationTimelineItem | None:
    if _is_zero_distance_transport(item):
        return None
    duration = _duration_minutes(item)
    map_url, map_label = _map_action(item, evidence)
    subtitle = _subtitle(item)
    return PresentationTimelineItem(
        id=item.id,
        kind=item.kind,
        icon=_ITEM_ICONS.get(item.kind, "•"),
        start_time=_display_time(item.start_time),
        end_time=_display_time(item.end_time),
        duration_minutes=duration,
        title=item.title,
        subtitle=subtitle,
        description=item.description or None,
        estimated_cost=_money_range(item.estimated_cost)
        if item.estimated_cost
        else None,
        map_url=map_url,
        map_label=map_label,
        dietary_tags=list(context.dietary_restrictions) if item.kind == "meal" else [],
    )


def _alerts(plan: TripPlan) -> list[PresentationAlert]:
    validator_issues = plan.validation.warnings if plan.validation else []
    alerts = [
        PresentationAlert(
            severity="action_required"
            if issue.code in _ACTION_WARNING_CODES
            else "warning",
            message=issue.message,
            day_number=issue.day_number,
            item_id=issue.item_id,
        )
        for issue in validator_issues
    ]
    known_messages = {alert.message.strip().casefold() for alert in alerts}
    for warning in plan.warnings:
        if warning.strip().casefold() not in known_messages:
            alerts.append(PresentationAlert(severity="advisory", message=warning))
            known_messages.add(warning.strip().casefold())
    return alerts


def _budget(plan: TripPlan) -> PresentationBudget:
    budget = plan.budget
    categories = {
        category.replace("_", " ").title(): _money_range(cost)
        for category, cost in budget.category_totals.items()
    }
    summary = (
        f"{_money(budget.estimated_minimum, budget.currency_code)} – "
        f"{_money(budget.estimated_maximum, budget.currency_code)} "
        f"against a {_money(budget.total_budget, budget.currency_code)} budget"
    )
    return PresentationBudget(
        currency_code=budget.currency_code,
        estimated_minimum=budget.estimated_minimum,
        estimated_maximum=budget.estimated_maximum,
        total_budget=budget.total_budget,
        within_budget=budget.within_budget,
        summary=summary,
        categories=categories,
    )


def _map_action(
    item: PlanItem, evidence: EvidenceBundle
) -> tuple[str | None, str | None]:
    if item.place is not None:
        if item.place.google_maps_url:
            return item.place.google_maps_url, "View on Google Maps"
        if item.place.provider_place_id:
            query = quote_plus(f"{item.place.latitude},{item.place.longitude}")
            place_id = quote_plus(item.place.provider_place_id)
            return (
                "https://www.google.com/maps/search/?api=1"
                f"&query={query}&query_place_id={place_id}",
                "View on Google Maps",
            )
    if item.travel_from_previous is not None:
        leg = item.travel_from_previous
        origin = (
            f"{leg.origin_location.latitude},{leg.origin_location.longitude}"
            if leg.origin_location is not None
            else _location_reference(leg.origin_label, evidence)
        )
        destination = (
            f"{leg.destination_location.latitude},{leg.destination_location.longitude}"
            if leg.destination_location is not None
            else _location_reference(leg.destination_label, evidence)
        )
        mode = quote_plus(_google_travel_mode(leg.mode))
        return (
            "https://www.google.com/maps/dir/?api=1"
            f"&origin={quote_plus(origin)}&destination={quote_plus(destination)}&travelmode={mode}",
            "Navigate",
        )
    return None, None


def _google_travel_mode(mode: str) -> str:
    normalized = mode.casefold()
    if normalized in {"walking", "walk", "foot-walking"}:
        return "walking"
    if normalized in {"cycling", "bicycle", "bike", "cycling-regular"}:
        return "bicycling"
    if normalized in {"transit", "public-transport", "train", "rail", "bus"}:
        return "transit"
    return "driving"


def _location_reference(label: str, evidence: EvidenceBundle) -> str:
    place = _matching_place(label, evidence)
    if place is not None and place.location is not None:
        return f"{place.location.latitude},{place.location.longitude}"
    return label


def _matching_place(label: str, evidence: EvidenceBundle) -> PlaceEvidence | None:
    target = label.casefold()
    exact = [
        place
        for place in evidence.places
        if target in {place.name.casefold(), place.stop_name.casefold()}
    ]
    if not exact:
        return None
    return next((place for place in exact if place.is_primary_destination), exact[0])


def _subtitle(item: PlanItem) -> str | None:
    if item.travel_from_previous is not None:
        leg = item.travel_from_previous
        parts = [leg.mode.title()]
        if leg.distance_km is not None:
            parts.append(f"{leg.distance_km:.1f} km")
        return " · ".join(parts)
    if item.kind == "meal":
        return "Meal break planned around the current route"
    if item.kind in {"rest", "free_time"}:
        return "Planned downtime for group comfort"
    return item.place.address if item.place and item.place.address else None


def _duration_minutes(item: PlanItem) -> int:
    start = datetime.combine(datetime.min.date(), item.start_time)
    end = datetime.combine(datetime.min.date(), item.end_time)
    return max(0, round((end - start).total_seconds() / 60))


def _is_zero_distance_transport(item: PlanItem) -> bool:
    return bool(
        item.kind == "transport"
        and item.travel_from_previous is not None
        and item.travel_from_previous.distance_km is not None
        and item.travel_from_previous.distance_km <= SAME_LOCATION_DISTANCE_KM
    )


def _display_time(value: time) -> str:
    return datetime.combine(datetime.min.date(), value).strftime("%I:%M %p").lstrip("0")


def _money_range(value: MoneyRange) -> str:
    if value.minimum == value.maximum:
        return _money(value.minimum, value.currency_code)
    return (
        f"{_money(value.minimum, value.currency_code)} – "
        f"{_money(value.maximum, value.currency_code)}"
    )


def _money(value: Decimal, currency_code: str) -> str:
    prefix = "₹" if currency_code.upper() == "INR" else f"{currency_code.upper()} "
    return f"{prefix}{value:,.0f}"


def _traveller_label(context: PlanningContext) -> str:
    parts = [f"{context.num_adults} adult{'s' if context.num_adults != 1 else ''}"]
    if context.num_children:
        parts.append(
            f"{context.num_children} child{'ren' if context.num_children != 1 else ''}"
        )
    if context.num_seniors:
        parts.append(
            f"{context.num_seniors} senior{'s' if context.num_seniors != 1 else ''}"
        )
    return ", ".join(parts)


def _checklist(plan: TripPlan, alerts: list[PresentationAlert]) -> list[str]:
    checklist = [
        "Book the vehicle and confirm driver, fuel, parking, and pickup details.",
        "Save the navigation links for each day for offline access.",
        "Carry water, medicines, comfortable footwear, and a phone power bank.",
    ]
    alert_text = " ".join(alert.message.casefold() for alert in alerts)
    if "opening" in alert_text:
        checklist.append(
            "Confirm the listed temple opening hours before leaving each morning."
        )
    if "budget" in alert_text or "estimate" in alert_text:
        checklist.append(
            "Confirm hotel and transport prices before booking to stay within budget."
        )
    if "temple" in f"{plan.title} {plan.summary}".casefold():
        checklist.append(
            "Pack modest clothing and easy-to-remove footwear for temple visits."
        )
    return checklist


def _markdown(presentation: TripPresentation) -> str:
    lines = [
        f"# {presentation.title}",
        "",
        presentation.summary,
        "",
        "## Trip overview",
        "",
    ]
    lines.extend(
        f"- **{fact.label}:** {fact.value}" for fact in presentation.quick_facts
    )
    lines.extend(
        [
            f"- **Validation:** {presentation.validation_status}",
            "",
            "## Action required",
            "",
        ]
    )
    if presentation.alerts:
        lines.extend(f"- ⚠️ {alert.message}" for alert in presentation.alerts)
    else:
        lines.append("- No active alerts.")
    lines.extend(["", "## Cost and budget", "", presentation.budget.summary, ""])
    for category, amount in presentation.budget.categories.items():
        lines.append(f"- **{category}:** {amount}")
    for day in presentation.days:
        lines.extend(
            [
                "",
                f"## Day {day.day_number} — {day.date_label}",
                "",
                f"**Base:** {day.base_stop}  ",
                f"**Daily estimate:** {day.daily_cost}",
                "",
            ]
        )
        for item in day.timeline:
            line = (
                f"- {item.icon} **{item.start_time}–{item.end_time}** "
                f"({item.duration_minutes} min): **{item.title}**"
            )
            if item.subtitle:
                line += f" — {item.subtitle}"
            lines.append(line)
            if item.estimated_cost:
                lines.append(f"  - Estimated cost: {item.estimated_cost}")
            if item.dietary_tags:
                lines.append(f"  - Dietary needs: {', '.join(item.dietary_tags)}")
            if item.map_url:
                lines.append(f"  - [{item.map_label}]({item.map_url})")
    if presentation.stays:
        lines.extend(["", "## Accommodation strategy", ""])
        for stay in presentation.stays:
            lines.append(
                f"- **{stay.stop}:** {stay.nights} night(s), {stay.estimated_cost}. "
                f"{stay.description}"
            )
    lines.extend(["", "## Pre-trip checklist", ""])
    lines.extend(f"- {item}" for item in presentation.checklist)
    return "\n".join(lines).strip() + "\n"
