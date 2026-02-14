"""User-facing itinerary wording derived only from evidence and requirements."""

from __future__ import annotations

from app.models.draft import DraftActivity
from app.models.evidence import PlaceEvidence
from app.models.context import PlanningContext, ResolvedDestination


def safe_plan_title(
    context: PlanningContext,
    destination: ResolvedDestination,
) -> str:
    """Build a title from confirmed input rather than free-form model text."""

    if context.trip_name:
        return context.trip_name
    stops = ", ".join(stop.name for stop in destination.stops)
    return f"Trip to {stops}"


def safe_plan_summary(
    context: PlanningContext,
    destination: ResolvedDestination,
) -> str:
    """Describe only facts contained in the planner input and resolver output."""

    travellers = "traveller" if context.traveller_count == 1 else "travellers"
    stops = ", ".join(stop.name for stop in destination.stops)
    return (
        f"A {context.duration_days}-day {context.travel_style} itinerary for "
        f"{context.traveller_count} {travellers}, covering {stops}."
    )


def safe_day_theme(context: PlanningContext, base_stop: str) -> str:
    """Use confirmed interests for the small heading shown above one day."""

    interests = ", ".join(context.activity_interests[:2])
    return f"{base_stop}: {interests}" if interests else base_stop


def safe_title(
    activity: DraftActivity,
    place: PlaceEvidence | None,
    stop_name: str,
    context: PlanningContext,
) -> str:
    if place is not None:
        return place.name
    if activity.kind == "meal":
        cuisines = ", ".join(context.cuisine_preferences[:2])
        prefix = f"{cuisines} " if cuisines else ""
        return f"{prefix}meal break in {stop_name}"
    if activity.kind == "rest":
        return f"Rest break in {stop_name}"
    if activity.kind == "free_time":
        if is_requested_shopping_time(activity, context):
            return f"Relaxed shopping time in {stop_name}"
        return f"Flexible time in {stop_name}"
    return activity.title


def safe_description(
    activity: DraftActivity,
    place: PlaceEvidence | None,
    context: PlanningContext,
) -> str:
    if place is not None:
        category = place.categories[0] if place.categories else "place"
        return (
            f"Visit this evidence-backed {category} at a {context.travel_style} pace. "
            "Use the separate visit-time range and confirm tickets before travel."
        )
    if activity.kind == "meal":
        needs = ", ".join(context.dietary_restrictions)
        suffix = f" Apply these dietary needs: {needs}." if needs else ""
        cuisines = ", ".join(context.cuisine_preferences[:2])
        cuisine_text = f" serving {cuisines} food" if cuisines else ""
        return (
            f"A flexible meal window; choose a suitable nearby local venue{cuisine_text}. "
            "Verify its current menu, prices, opening hours, and dietary fit." + suffix
        )
    if activity.kind == "rest":
        return "Time reserved for sitting, washrooms, and slower movement."
    if activity.kind == "free_time":
        if is_requested_shopping_time(activity, context):
            return (
                "Keep this period unhurried for local shopping. Choose a nearby "
                "market and confirm its current hours before going."
            )
        return "Uncommitted time that can absorb delays or optional exploration."
    return activity.description


def is_requested_shopping_time(
    activity: DraftActivity,
    context: PlanningContext,
) -> bool:
    draft_text = f"{activity.title} {activity.description}".casefold()
    requested_text = " ".join(
        [*context.activity_interests, *context.trip_context_notes]
    ).casefold()
    return any(word in draft_text for word in ("shopping", "market", "bazaar")) and any(
        word in requested_text for word in ("shopping", "market", "bazaar")
    )
