"""Anonymous sample exports and local review pages."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

from app.phase_two.persistence import durable_plan
from app.phase_two.portfolio import render_plan
from app.phase_two.runtime import json_value


def anonymous_plan(plan, sample_id):
    result = deepcopy(plan)
    result.update(
        trip_id=0, run_id=f"sample:{sample_id}", plan_id=f"sample:{sample_id}"
    )
    return result


def export_sample(directory, scenario, messages, memory, plan, usage):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    plan = anonymous_plan(plan, scenario["id"])
    expected = scenario["expected"]
    summary = plan["trip_summary"]
    checks = {
        "days": len(plan["days"]) == expected["days"],
        "dates": str(summary["start_date"]) == expected["start_date"]
        and str(summary["end_date"]) == expected["end_date"],
        "currency": memory.metadata.currency_code == expected["currency"],
        "budget": memory.needed.trip_budget == expected["budget"],
        "adults": memory.needed.num_adults == expected["adults"],
        "children": (memory.optional.num_children or 0) == expected["children"],
        "activities_named": all(
            e.get("title") and not e["title"].startswith("Selected place")
            for day in plan["days"]
            for e in day.get("events", [])
            if e.get("kind") == "activity"
        ),
    }
    warnings = plan.get("warnings", [])
    errors = [w for w in warnings if w.get("severity") == "error"]
    report = {
        "checks": checks,
        "plan_status": plan["status"],
        "blocking_issues": errors,
        "warnings": warnings,
        "publication_status": "needs_correction"
        if errors or not all(checks.values())
        else "needs_review",
        "reason": "Resolve blocking issues before public use."
        if errors
        else "Review the actual itinerary and image/source permissions before public use.",
    }
    packet = {
        "schema_version": "sample_trip.v1",
        "scope": "generated_recommendations_not_verified_bookings",
        "id": scenario["id"],
        "title": scenario["title"],
        "features": scenario["features"],
        "generated_at": datetime.now(UTC).isoformat(),
        "conversation": [
            {"role": m.role, "content": m.content}
            for m in messages
            if m.role in {"user", "assistant"}
        ],
        "requirements": {
            "needed": memory.needed.to_value_dict(),
            "preferences": memory.optional.to_value_dict(),
            "notes": list(
                dict.fromkeys(
                    [
                        *(getattr(memory.optional, "trip_notes", None) or []),
                        *getattr(memory.internal, "trip_context_notes", []),
                    ]
                )
            ),
            "confirmation_summary": next(
                (
                    m.content
                    for m in reversed(messages)
                    if m.role == "assistant" and "Does this look right?" in m.content
                ),
                None,
            ),
        },
        "plan": durable_plan(plan),
        "review": report,
        "usage": usage,
    }
    write_json(directory / "sample.json", packet)
    # Provider display content is review-only, not a permanent public fixture.
    write_json(
        directory / "review.json",
        {
            "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            "publication_status": "private_review_only",
            "plan": plan,
            "review": report,
        },
    )
    html = render_plan(plan)
    notice = '<p class="banner">Private generated preview. Provider display details are temporary; refresh them before later use. This sample has not been approved for public display.</p>'
    (directory / "review.html").write_text(
        html.replace("<main>", "<main>" + notice, 1), encoding="utf-8"
    )
    return report


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(json_value(payload), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def write_manifest(output, scenarios):
    """Rebuild the catalog so resuming one case preserves the other samples."""
    output = Path(output)
    runs = []
    for scenario in scenarios:
        folder = output / scenario["id"]
        if (folder / "run.json").exists():
            run = json.loads((folder / "run.json").read_text(encoding="utf-8"))
            if (folder / "sample.json").exists():
                packet = json.loads(
                    (folder / "sample.json").read_text(encoding="utf-8")
                )
                report = packet["review"]
                if report["blocking_issues"] or not all(report["checks"].values()):
                    report.update(
                        publication_status="needs_correction",
                        reason="Resolve blocking issues before public use.",
                    )
                    write_json(folder / "sample.json", packet)
                run["review"] = report
            runs.append(run)
    write_json(
        output / "manifest.json",
        {"generated_at": datetime.now(UTC).isoformat(), "samples": runs},
    )
