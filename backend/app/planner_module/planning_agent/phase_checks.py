"""Mechanical draft checks; semantic selection remains with the planner."""
from collections import Counter
from datetime import date, timedelta

from ..models.planning import PlannerInput
from .contracts import PhaseCheck, PhaseIssue, TripDraft


def reached_destinations(timelines: dict, data: PlannerInput) -> set[str]:
    reached = {row[key] for result in timelines.values() for row in result["timeline"]
               for key in ("origin_id", "destination_id")}
    return reached | {poi.stop_id for poi in data.pois if poi.id in reached}


def check_draft(draft: TripDraft, data: PlannerInput, *, phase: str,
                explicit_order: tuple[str, ...] = (), registered_anchors=(), anchor_bases=None) -> PhaseCheck:
    """Order restrictions must be supplied from confirmed constraints, not list order."""
    if phase not in {"skeleton", "city_details", "repair"}:
        raise ValueError("Unknown planning phase")
    result = PhaseCheck()

    def issue(code, message, *, unknown=False, day=None, ids=()):
        result.issues.append(PhaseIssue(code=code, message=message,
            outcome="unknown" if unknown else "fail", day_number=day, entity_ids=list(ids)))

    start, end = date.fromisoformat(data.start_date), date.fromisoformat(data.end_date)
    if (end - start).days + 1 != data.duration_days:
        issue("INPUT_DATE_CONFLICT", "Confirmed dates and duration disagree.")
    expected = list(range(1, data.duration_days + 1))
    if [day.day_number for day in draft.days] != expected:
        issue("DAY_COVERAGE", "Exactly one chronologically ordered draft is required per day.")
    stops = {stop.id: stop for stop in data.stops}
    pois = {poi.id: poi for poi in data.pois}
    anchors = set(stops) | set(pois) | {"origin_anchor", "end_anchor"} | set(registered_anchors)
    if not set(draft.base_sequence) <= set(stops):
        issue("UNKNOWN_BASE", "Declared bases must be registered researched stops.")
    if len(draft.base_sequence) != len(set(draft.base_sequence)):
        issue("DUPLICATE_BASE", "Base sequence contains duplicate identities.")
    if explicit_order:
        positions = [draft.base_sequence.index(item) for item in explicit_order if item in draft.base_sequence]
        if len(positions) != len(explicit_order) or positions != sorted(positions):
            issue("EXPLICIT_ORDER", "Confirmed ordered subsequence must be preserved.", ids=explicit_order)
    selected = [pid for day in draft.days for pid in day.selected_poi_ids]
    duplicates = [pid for pid, count in Counter(selected).items() if count > 1]
    if duplicates:
        issue("DUPLICATE_VISIT", "Repeated visits require distinct supported visit-instance records.", ids=duplicates)
    if set(selected) - set(pois):
        issue("UNKNOWN_POI", "Visits must use the frozen researched catalog.", ids=set(selected) - set(pois))
    visited = set(draft.base_sequence) | {pois[pid].stop_id for pid in selected if pid in pois}
    for req in data.requirements:
        if not req.required:
            continue
        targets = {stop.id for stop in data.stops if req.id in stop.requirement_ids}
        required_pois = {poi.id for poi in data.pois if req.id in poi.requirement_ids}
        if req.requirement_type == "destination_or_base":
            if not (targets & visited):
                issue("REQUIRED_DESTINATION", "Required destination is not represented.", ids=targets or (req.id,))
            continue
        if req.requirement_type not in {"exact_poi", "all_route_members"} or not required_pois:
            issue("REQUIREMENT_UNRESOLVED", "Required visit has no supported catalog mapping.", unknown=True, ids=[req.id])
            continue
        if req.requirement_type == "all_route_members":
            unmapped_members = {stop.id for stop in data.stops if stop.role == "route_member" and req.id in stop.requirement_ids} - {pois[pid].stop_id for pid in required_pois}
            if unmapped_members:
                issue("REQUIREMENT_UNRESOLVED", "Circuit members lack their required visit records.", unknown=True, ids=unmapped_members)
        if phase != "skeleton" and required_pois - set(selected):
            issue("REQUIRED_VISIT", "Required visit is not allocated; selection alone still needs timeline validation.", ids=required_pois - set(selected))
    if phase != "skeleton":
        missing = {poi.id for poi in data.pois if poi.is_mandatory} - set(selected)
        if missing:
            issue("MANDATORY_POI_UNALLOCATED", "Mandatory visits must be allocated in a detailed draft.", ids=missing)
    nights = Counter()
    for index, day in enumerate(draft.days):
        number = day.day_number
        if day.date != start + timedelta(days=number - 1):
            issue("DAY_DATE", "Day date disagrees with the confirmed trip.", day=number)
        for anchor in (day.start_anchor_id, day.end_anchor_id, day.overnight_anchor_id):
            if anchor is not None and anchor not in anchors:
                issue("UNKNOWN_ANCHOR", "Traveler state references an unregistered anchor.", day=number, ids=[anchor])
        if day.start_anchor_id is None or day.end_anchor_id is None:
            issue("ANCHOR_UNKNOWN", "Day start/end state needs confirmation.", unknown=True, day=number)
        if index == 0 and day.start_anchor_id != "origin_anchor":
            issue("ORIGIN", "First day must begin at the confirmed origin.", day=number)
        if index == len(draft.days) - 1 and day.end_anchor_id != "end_anchor":
            issue("ENDPOINT", "Last day must reach the confirmed end location.", day=number)
        if index and draft.days[index - 1].end_anchor_id != day.start_anchor_id:
            issue("BOUNDARY_DISCONTINUITY", "Previous end and next start do not match.", day=number)
        if index < len(draft.days) - 1:
            if day.overnight_kind == "stay":
                if day.overnight_anchor_id is None or day.overnight_anchor_id != day.end_anchor_id:
                    issue("OVERNIGHT_STATE", "Stay anchor must match the end-of-day location.", day=number)
                else:
                    nights[(anchor_bases or {}).get(day.overnight_anchor_id) or day.overnight_anchor_id] += 1
            elif day.overnight_kind == "unknown":
                issue("OVERNIGHT_UNKNOWN", "Overnight stay or travel remains unknown.", unknown=True, day=number)
            elif day.overnight_kind == "none":
                issue("OVERNIGHT_MISSING", "An intermediate night needs an explicit stay or in-transit state.", unknown=True, day=number)
        if day.preferred_order and Counter(day.preferred_order) != Counter(day.selected_poi_ids):
            issue("SELECTED_SET", "Ordering cannot add or drop selected visits.", day=number)
        for pid, allowance in day.visit_allowances.items():
            if pid not in day.selected_poi_ids or pid not in pois:
                issue("ALLOWANCE_TARGET", "Allowance must reference a selected registered visit.", day=number, ids=[pid])
                continue
            minimum = pois[pid].minimum_visit_minutes
            if minimum and allowance.minutes < minimum:
                issue("VISIT_MINIMUM", "Allowance cannot shorten a researched minimum.", day=number, ids=[pid])
            if allowance.basis == "research" and (not allowance.evidence_ids or not set(allowance.evidence_ids) <= set(pois[pid].evidence_ids)):
                issue("ALLOWANCE_EVIDENCE", "Research duration requires registered evidence for this visit.", day=number, ids=[pid])
    if dict(nights) != {key: value for key, value in draft.nights_by_base.items() if value}:
        issue("NIGHT_ALLOCATION", "Declared stay nights must match explicit overnight states, excluding in-transit nights.")
    result.can_advance_to_details = phase == "skeleton" and not any(item.outcome == "fail" for item in result.issues)
    return result
