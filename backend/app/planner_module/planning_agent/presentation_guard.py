"""Conservative text attachment without changing the checked itinerary."""
from .agent_outputs import PresentationResult
from .contracts import fingerprint
from .route_tool import stay_anchor_id


def presentation_options(artifact, data):
    draft = artifact.get("draft", {})
    names = {item.id: item.name for item in data.pois}
    introduction = ("Your itinerary is ready to review. Check the practical notes before booking."
        if artifact.get("status") == "READY" else
        "This is a provisional itinerary. Review the unresolved details before booking or travelling.")
    days = []
    for day in draft.get("days", []):
        selected = day.get("selected_poi_ids", [])
        visits = "; ".join(names[pid] for pid in selected if pid in names)
        summary = (f"Planned visits: {visits}. See the schedule and its qualifications below."
                   if visits else "See this day's travel, rest and timing details below.")
        days.append({"day_number": day["day_number"], "entity_ids": selected,
                     "summary": summary, "summary_parts": []})
    return {"checked_draft_hash": fingerprint(draft), "introduction": introduction, "days": days, "practical_notes": []}


def presentation_catalog(artifact, data):
    """Offer grounded sections for editorial selection without an extra fact writer."""
    default = presentation_options(artifact, data)
    validation = artifact.get("validation", {})
    names = {item.id: item.name for item in [*data.stops, *data.pois]}
    names.update(origin_anchor=data.origin, end_anchor=data.end_location or data.origin)
    names.update({stay_anchor_id(stay): stay["name"] for stay in data.stay_bases if stay.get("name")})
    days = {}
    for day, copy in zip(artifact.get("draft", {}).get("days", []), default["days"]):
        sections = [copy["summary"], f"Date: {day['date']}."]
        rows = validation.get("timelines", {}).get(str(day["day_number"]), {}).get("timeline", [])
        if rows:
            start, end = names.get(rows[0]["origin_id"]), names.get(rows[-1]["destination_id"])
            if start and end:
                sections.append(f"Planned start: {start}. Planned finish: {end}.")
            if any(not row.get("start") or not row.get("end") for row in rows):
                sections.append("Some timings remain unresolved; do not treat the full day as a confirmed schedule.")
        if day.get("overnight_kind") == "stay" and names.get(day.get("overnight_anchor_id")):
            sections.append(f"Planned overnight base: {names[day['overnight_anchor_id']]}. This is not a hotel booking.")
        elif day.get("overnight_kind") == "in_transit":
            sections.append("The draft places this night in transit; verify the journey arrangements before booking.")
        days[str(day["day_number"])] = {"sections": sections, "required_section": copy["summary"]}
    notes = list(dict.fromkeys(row["message"] for row in [*validation.get("issues", []), *validation.get("disclosures", [])]
                              if isinstance(row.get("message"), str) and row["message"]))
    return {"introductions": [default["introduction"], f"Your {len(default['days'])}-day itinerary. {default['introduction']}"],
            "days": days, "practical_notes": notes}


def validate_presentation(value, artifact, data):
    """Validate editorial choices separately from authoritative itinerary facts."""
    result = PresentationResult.model_validate(value)
    expected = presentation_options(artifact, data)
    entities = {day["day_number"]: set(day["entity_ids"]) for day in expected["days"]}
    result.check_targets(draft_hash=expected["checked_draft_hash"], day_entities=entities)
    catalog = presentation_catalog(artifact, data)
    if result.introduction not in catalog["introductions"]:
        raise ValueError("Unsupported presentation introduction")
    if [day.day_number for day in result.days] != [day["day_number"] for day in expected["days"]]:
        raise ValueError("Presentation must retain every day in itinerary order")
    for day, fallback in zip(result.days, expected["days"]):
        sections = catalog["days"][str(day.day_number)]
        if day.entity_ids != fallback["entity_ids"]:
            raise ValueError("Presentation must retain the selected visit identities")
        if day.summary_parts:
            if (len(set(day.summary_parts)) != len(day.summary_parts)
                    or not set(day.summary_parts) <= set(sections["sections"])
                    or sections["required_section"] not in day.summary_parts
                    or day.summary != " ".join(day.summary_parts)):
                raise ValueError("Summary differs from its grounded selected sections")
        elif day.summary != fallback["summary"]:
            raise ValueError("Unsupported presentation day text")
    if (len(set(result.practical_notes)) != len(result.practical_notes)
            or not set(result.practical_notes) <= set(catalog["practical_notes"])):
        raise ValueError("Practical notes differ from checked findings")
    return result.model_dump(mode="json")
