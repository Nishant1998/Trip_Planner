"""Thin structural V2 report validator; it deliberately does not rank travel options."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models.research import ResearchQuality, ResearchReport
from ..tools.transport import TERMINAL_TYPES


@dataclass(frozen=True)
class ResearchValidationResult:
    outcome: str  # pass | repair | accept_partial | blocked
    planner_usable: bool
    issues: list[dict[str, str]]


def _normal(value: str) -> str:
    return " ".join(value.casefold().split())


def _is_terminal(stop: Any) -> bool:
    return stop.role == "terminal" or bool(
        TERMINAL_TYPES.intersection(
            {
                str(stop.type or "").casefold(),
                str(stop.provider_primary_type or "").casefold(),
                *{str(value).casefold() for value in stop.provider_types},
            }
        )
    )


def validate_report(report: ResearchReport, trip: Any, repair_count: int, max_repairs: int) -> ResearchValidationResult:
    issues: list[dict[str, str]] = []
    if report.status == "blocked":
        issues.append({"code": "RESEARCHER_BLOCKED", "severity": "blocked", "message": "The researcher reported that no coherent planning handoff is available."})
    if report.normalized_trip_context.model_dump(mode="json") != trip.model_dump(mode="json"):
        issues.append({"code": "INPUT_CHANGED", "severity": "blocked", "message": "Confirmed trip input changed in the submitted report."})

    bases = [stop for stop in report.resolved_stops if stop.role == "base"]
    explicit_route = trip.destination.type == "route" and len(trip.destination.items) > 1
    if explicit_route:
        expected = [f"destination:{index}" for index, _ in enumerate(trip.destination.items)]
        represented_ids = {
            requirement_id
            for stop in report.resolved_stops
            for requirement_id in stop.requirement_ids
        }
        for requirement_id, item in zip(expected, trip.destination.items, strict=True):
            if requirement_id not in represented_ids:
                issues.append({"code": "EXPLICIT_ROUTE_ITEM_MISSING", "severity": "repair", "message": f"Confirmed route item {item!r} is not represented."})
        requested = set(expected)
        for base in bases:
            if not set(base.requirement_ids) & requested:
                issues.append({"code": "UNREQUESTED_PRIMARY_BASE", "severity": "repair", "message": f"{base.name!r} was added as a primary base to an explicit route."})

    for base in bases:
        candidates = report.pois_by_stop.get(base.id, [])
        if not candidates:
            issues.append({"code": "BASE_WITHOUT_CANDIDATES", "severity": "repair", "message": f"Primary base {base.name!r} has no research candidates."})
    if not report.resolved_stops or not any(report.pois_by_stop.values()):
        issues.append({"code": "NO_COHERENT_PLANNING_WORLD", "severity": "blocked", "message": "No resolved places and usable candidates exist for planning."})

    for index, must_see in enumerate(trip.must_see):
        requirement_id = f"must_see:{index}"
        matching_pois = [
            poi
            for pois in report.pois_by_stop.values()
            for poi in pois
            if requirement_id in poi.requirement_ids
        ]
        represented = bool(matching_pois)
        unresolved = any(must_see.casefold() in gap.casefold() for gap in report.unresolved_gaps)
        if not represented and not unresolved:
            issues.append({"code": "MUST_SEE_UNREPRESENTED", "severity": "repair", "message": f"Required item {must_see!r} is neither represented nor explicitly unresolved."})
        if matching_pois and all(
            poi.business_status in {"CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"}
            or not poi.is_recommendable
            for poi in matching_pois
        ) and not unresolved:
            issues.append({"code": "MUST_SEE_CLOSED", "severity": "repair", "message": f"Required item {must_see!r} is reported unavailable and needs an explicit explanation."})

    transport = report.transport_requirements
    if len(bases) > 1 and not transport:
        issues.append({"code": "TRANSPORT_UNREPRESENTED", "severity": "repair", "message": "Multiple bases have no transport research representation."})
    expects_major = bool(trip.transport_preferences.long_distance_modes)
    expects_local = bool(trip.transport_preferences.local_modes)
    intercity = [item for item in transport if item.leg_kind == "intercity"]
    if expects_major and len(intercity) < max(0, len(bases) - 1):
        issues.append({"code": "INTERCITY_TRANSPORT_INCOMPLETE", "severity": "repair", "message": "Every consecutive destination-base change needs a distinct transport requirement."})
    first_base = bases[0] if bases else None
    last_base = bases[-1] if bases else None
    if expects_major and first_base and _normal(trip.origin) != _normal(first_base.name) and not any(
        item.leg_kind == "entry" for item in transport
    ):
        issues.append({"code": "ENTRY_TRANSPORT_MISSING", "severity": "repair", "message": "Origin-to-first-destination transport is not represented."})
    requested_end = trip.end_location
    if expects_major and requested_end and last_base and _normal(requested_end) != _normal(last_base.name) and not any(
        item.leg_kind == "exit" for item in transport
    ):
        issues.append({"code": "EXIT_TRANSPORT_MISSING", "severity": "repair", "message": "Final-destination-to-end-location transport is not represented."})

    stop_by_id = {stop.id: stop for stop in report.resolved_stops}
    local_pairs = {
        frozenset((item.origin_id, item.destination_id))
        for item in transport
        if item.leg_kind == "local_access"
        and item.origin_id
        and item.destination_id
    }
    destination_terminal_ids: set[str] = set()
    for item in transport:
        if item.leg_kind not in {"entry", "intercity", "exit"}:
            continue
        major_mode = any(
            token in item.preferred_mode.casefold()
            for token in ("flight", "air", "train", "rail")
        )
        if major_mode:
            endpoints = [
                stop_by_id.get(item.origin_id),
                stop_by_id.get(item.destination_id),
            ]
            if not all(endpoint is not None and _is_terminal(endpoint) for endpoint in endpoints):
                issues.append({"code": "MAJOR_TRANSPORT_TERMINAL_UNRESOLVED", "severity": "repair", "message": f"{item.origin_label} → {item.destination_label} needs provider-typed terminal endpoints."})
            if item.leg_kind == "entry" and item.destination_id:
                destination_terminal_ids.add(item.destination_id)
            elif item.leg_kind == "exit" and item.origin_id:
                destination_terminal_ids.add(item.origin_id)
            elif item.leg_kind == "intercity":
                destination_terminal_ids.update(
                    value for value in (item.origin_id, item.destination_id) if value
                )
        if item.status in {"missing", "unavailable"}:
            issues.append({"code": "MAJOR_TRANSPORT_EVIDENCE_MISSING", "severity": "repair", "message": f"{item.origin_label} → {item.destination_label} remains unresolved after research."})

    base_ids = {base.id for base in bases}
    for terminal_id in destination_terminal_ids:
        if expects_local and terminal_id in stop_by_id and _is_terminal(stop_by_id[terminal_id]) and not any(
            terminal_id in pair and bool(pair & base_ids) for pair in local_pairs
        ):
            issues.append({"code": "TERMINAL_LOCAL_ACCESS_MISSING", "severity": "repair", "message": f"Terminal {stop_by_id[terminal_id].name!r} has no researched local-access link to a destination base."})

    for pois in report.pois_by_stop.values():
        for poi in pois:
            if expects_local and poi.is_mandatory and frozenset((poi.stop_id, poi.id)) not in local_pairs:
                issues.append({"code": "MANDATORY_LOCAL_ACCESS_MISSING", "severity": "repair", "message": f"Mandatory place {poi.name!r} has no researched local-access requirement from its base."})
    if trip.travellers.accessibility_needs:
        requirement_ids = {f"must_see:{index}" for index, _ in enumerate(trip.must_see)}
        relevant = [
            poi
            for pois in report.pois_by_stop.values()
            for poi in pois
            if poi.is_mandatory or set(poi.requirement_ids) & requirement_ids
        ]
        fact_represents_accessibility = any(
            "accessibility" in str(fact).casefold()
            for fact in report.current_facts
        )
        if relevant and not fact_represents_accessibility and not all(
            poi.accessibility_notes or "accessibility" in poi.unresolved_fields
            for poi in relevant
        ):
            issues.append({"code": "ACCESSIBILITY_UNREPRESENTED", "severity": "repair", "message": "Mandatory places lack accessibility evidence or explicit unknown status."})

    blocked = [issue for issue in issues if issue["severity"] == "blocked"]
    if blocked:
        return ResearchValidationResult("blocked", False, issues)
    repair = [issue for issue in issues if issue["severity"] == "repair"]
    if repair and repair_count < max_repairs:
        return ResearchValidationResult("repair", True, issues)
    if repair:
        return ResearchValidationResult("accept_partial", True, issues)
    return ResearchValidationResult("pass", True, issues)


def finalise_report(report: ResearchReport, result: ResearchValidationResult, metrics: Any, *, architecture_version: str, prompt_version: str) -> ResearchReport:
    gaps = list(dict.fromkeys(report.unresolved_gaps + [issue["message"] for issue in result.issues]))
    warnings = [issue["message"] for issue in result.issues if issue["severity"] == "warning"]
    quality = ResearchQuality(
        completeness_score=(1.0 if not warnings else 0.85) if result.outcome == "pass" else (0.65 if result.planner_usable else 0.0),
        critical_gaps=[] if result.planner_usable else gaps,
        optional_gaps=warnings if result.outcome == "pass" else gaps,
        sufficient_for_planning=result.planner_usable,
    )
    coverage = report.coverage
    if coverage is not None:
        coverage = coverage.model_copy(update={"sufficient_for_planning": result.planner_usable})
    status = report.status if result.outcome == "pass" else ("partial" if result.planner_usable else "blocked")
    return report.model_copy(update={
        "status": status,
        "quality": quality,
        "coverage": coverage,
        "unresolved_gaps": gaps,
        "metrics": metrics,
        "architecture_version": architecture_version,
        "prompt_version": prompt_version,
    })
