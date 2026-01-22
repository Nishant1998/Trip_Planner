"""Check measurable cost quantities against the committed trip, not model totals."""
from decimal import Decimal


def quantity_issues(draft, data):
    issues = []
    costs = list({item.id: item for item in draft.costs}.values())
    stays = sum(day.overnight_kind == "stay" for day in draft.days[:-1])
    meals = any(step.kind == "meal" for day in draft.days if day.timeline for step in day.timeline.steps)

    def add(code, category, message, outcome="fail"):
        issues.append({"code": code, "category": category, "message": message, "outcome": outcome})

    for category, applicable in (("accommodation", stays > 0), ("meals", meals)):
        rows = [item for item in costs if item.category == category]
        if applicable and rows and all(item.basis == "not_applicable" for item in rows):
            add("COST_CATEGORY_REQUIRED", category,
                f"{category} is used by the itinerary; confirm that the not-applicable cost really covers these stays or meals, or record a supported free service.", "unknown")

    accommodation = [item for item in costs if item.category == "accommodation"
                     and item.basis != "not_applicable"]
    if stays and accommodation and all(item.unit == "room_night" for item in accommodation):
        # One room is only a lower bound, never an inferred occupancy decision.
        rooms = data.accommodation_preferences.get("num_rooms")
        if rooms is None:
            minimum_rooms = 1
        elif isinstance(rooms, int) and not isinstance(rooms, bool) and rooms > 0:
            minimum_rooms = rooms
        else:
            minimum_rooms = None
            add("ROOM_COUNT_UNKNOWN", "accommodation", "Confirmed room count is not a positive integer.", "unknown")
        if minimum_rooms is not None and sum(item.quantity for item in accommodation) < stays * minimum_rooms:
            add("ROOM_NIGHTS_UNDERCOUNTED", "accommodation",
                "Accommodation room-night quantities do not cover the planned stay nights and confirmed room count.")

    food = [item for item in costs if item.category == "meals" and item.basis != "not_applicable"]
    adults, children = data.travellers.get("num_adults"), data.travellers.get("num_children", 0)
    counts_known = all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
                       for value in (adults, children))
    party = adults + children if counts_known else None
    for category in ("main_transport", "local_transport"):
        tickets = [item for item in costs if item.category == category and item.basis != "not_applicable"]
        if tickets and all(item.unit == "person" for item in tickets):
            if party is None or party == 0:
                add("TRANSPORT_PARTY_UNKNOWN", category,
                    "Per-person transport costs require confirmed adult and child counts.", "unknown")
            elif sum(item.quantity for item in tickets) < party:
                add("TRANSPORT_PASSENGERS_UNDERCOUNTED", category,
                    "Per-person transport costs do not cover even one journey for every traveler; free fares or package inclusions need explicit coverage.", "unknown")
        for item in tickets:
            if item.unit in {"person", "vehicle"} and item.quantity != item.quantity.to_integral_value():
                add("TRANSPORT_QUANTITY_FRACTIONAL", category,
                    "Transport charged per person or vehicle must use whole passenger or vehicle quantities.")
    if food and all(item.unit == "person_day" for item in food):
        if party is None or party == 0:
            add("MEAL_PARTY_UNKNOWN", "meals", "Person-day meal coverage needs the confirmed adult and child counts.", "unknown")
        else:
            required = Decimal(party) * data.duration_days
            if sum(item.quantity for item in food) < required:
                add("MEAL_PERSON_DAYS_UNDERCOUNTED", "meals",
                    "The person-day meal allowance does not cover all travelers and trip days; partial-day or package coverage must be represented explicitly.", "unknown")
    return issues


def apply_coverage_issues(budget, issues):
    """Invalidate unsupported affordability without discarding known cost subtotals."""
    if not issues:
        return
    categories = {item["category"] for item in issues}
    budget["coverage_issues"] = sorted(set(budget.get("coverage_issues", [])) | {item["code"] for item in issues})
    budget["unknown_categories"] = sorted(set(budget.get("unknown_categories", [])) | categories)
    budget["upper_estimate"] = None
    for category in categories:
        row = budget.get("categories", {}).get(category)
        if row is not None:
            row.update(status="unknown", upper_estimate=None)
    if budget.get("status") == "known":
        budget["status"] = "partial"
    if any(item["outcome"] == "fail" for item in issues):
        budget["outcome"] = "fail"
    elif budget.get("outcome") == "pass":
        budget["outcome"] = "unknown"
