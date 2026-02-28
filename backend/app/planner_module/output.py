from __future__ import annotations
from .models.planning import PlanningResult


def markdown(result: PlanningResult) -> str:
    lines = [
        f"# {result.trip_summary}",
        "",
        "## Trip Overview",
        "",
        f"Bases: {' → '.join(result.selected_bases) or 'Research did not resolve an overnight base.'}",
        f"Budget: {result.budget_summary.known_attraction_cost:.0f} {result.budget_summary.currency} known attraction cost ({result.budget_summary.status}).",
        "",
    ]
    for day in result.days:
        lines += [
            f"## Day {day.day_number} — {day.title}",
            "",
            f"**Start:** {day.start_anchor or 'Confirm start anchor'}",
            f"**Overnight:** {day.overnight_base or 'Confirm accommodation base'}",
            "",
            "| Time | Type | Item | Details |",
            "|---|---|---|---|",
        ]
        poi_names = {activity.poi_id: activity.name for activity in day.activities}
        for block in day.timeline:
            item = (
                f"{block.origin_id or ''} → {block.destination_id or ''}"
                if block.type.value in {"travel", "buffer", "unresolved_transfer"}
                else poi_names.get(block.poi_id, block.type.value)
            )
            details = "; ".join(block.notes) or block.status
            lines.append(
                f"| {block.start_time}–{block.end_time} | {block.type.value.replace('_', ' ').title()} | {item} | {block.duration_minutes if block.duration_minutes is not None else 'UNKNOWN'} min; {details} |"
            )
        if day.meals_rest:
            lines += ["", "### Meals / rest", *[f"- {x}" for x in day.meals_rest]]
        if day.confirm_before_visit:
            lines += [
                "",
                "### Confirm before visiting",
                *[f"- {x}" for x in day.confirm_before_visit],
            ]
        lines.append("")
    for title, items in [
        ("Assumptions & Uncertainties", result.assumptions + result.uncertainties),
        ("Relaxed Constraints", result.relaxed_constraints),
        ("Unsatisfied Requirements", result.unsatisfied_mandatory_requirements),
    ]:
        if items:
            lines += [f"## {title}", *[f"- {x}" for x in items], ""]
    lines += [
        "## Planning Metrics",
        "",
        *[f"- {k}: {v}" for k, v in result.planning_metrics.items()],
    ]
    return "\n".join(lines) + "\n"
