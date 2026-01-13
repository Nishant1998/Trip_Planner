"""Bounded LLM decision layers. Safe deterministic fallbacks keep planning usable."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import Counter
from math import asin, cos, radians, sin, sqrt
from types import SimpleNamespace
from typing import Any

import httpx
from openai import AsyncOpenAI

from app.services.llm_usage import (
    ensure_current_trip_can_start_model_call,
    record_current_llm_usage,
)

from .models.planning import (
    DayKind,
    DaySkeleton,
    ReviewerEdit,
    ReviewerFinding,
    TripStrategy,
    ValidationIssue,
)

logger = logging.getLogger(__name__)


def _distance_km(a, b) -> float:
    """Geographic fallback only; typed stop relationships always take priority."""
    lat1, lon1, lat2, lon2 = map(
        radians, [a.latitude, a.longitude, b.latitude, b.longitude]
    )
    return (
        6371
        * 2
        * asin(
            sqrt(
                sin((lat2 - lat1) / 2) ** 2
                + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
            )
        )
    )


def compatible_with_day(poi, day, world, local_attachment_km: float = 25.0) -> bool:
    """Stable-ID/base check, with a deliberately small coordinate fallback."""
    if poi.stop_id == day.base_stop_id:
        return True
    if (
        not day.base_stop_id
        or day.base_stop_id.startswith("overnight:")
        or day.base_stop_id == "end_anchor"
    ):
        return False
    stop = next(
        (
            item
            for item in getattr(world.input, "stops", [])
            if item.id == day.base_stop_id
        ),
        None,
    )
    return bool(
        stop and _distance_km(poi.coordinates, stop.coordinates) <= local_attachment_km
    )


class LLM:
    def __init__(self, model: str, effort: str):
        self.model, self.effort = model, effort
        self.metrics = {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "model": model,
        }
        key = os.getenv("OPENAI_API_KEY")
        self.client = (
            AsyncOpenAI(
                api_key=key,
                max_retries=int(os.getenv("PLANNER_LLM_MAX_RETRIES", "0")),
                base_url=os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1"),
                http_client=httpx.AsyncClient(
                    timeout=httpx.Timeout(
                        float(os.getenv("LLM_HTTP_TIMEOUT_SECONDS", "60"))
                    ),
                    trust_env=False,
                ),
            )
            if key
            else None
        )

    async def json(
        self, purpose: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not self.client:
            return None
        ensure_current_trip_can_start_model_call()
        try:
            # SDK retries can otherwise keep one bounded planner pass alive
            # indefinitely when a provider stalls.  Falling back to the frozen
            # deterministic planner is safer than holding an incomplete run.
            request = asyncio.create_task(
                self.client.responses.create(
                    model=self.model,
                    reasoning={"effort": self.effort},
                    input=f"{purpose}. Return JSON only.\n" + json.dumps(payload),
                    text={"format": {"type": "json_object"}},
                )
            )
            try:
                response = await asyncio.wait_for(
                    asyncio.shield(request),
                    timeout=float(os.getenv("PLANNER_LLM_TIMEOUT_SECONDS", "75")),
                )
            except asyncio.CancelledError:
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
                raise
            except asyncio.TimeoutError:
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
                self.metrics["timeout"] = True
                return None
            usage = response.usage
            record_current_llm_usage(
                "planner",
                self.model,
                SimpleNamespace(
                    requests=1,
                    input_tokens=getattr(usage, "input_tokens", 0),
                    output_tokens=getattr(usage, "output_tokens", 0),
                    cache_read_tokens=getattr(
                        getattr(usage, "input_tokens_details", None), "cached_tokens", 0
                    ),
                    details={
                        "reasoning_tokens": getattr(
                            getattr(usage, "output_tokens_details", None),
                            "reasoning_tokens",
                            0,
                        )
                    },
                ),
            )
            self.metrics["calls"] += 1
            self.metrics["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
            self.metrics["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
            return json.loads(response.output_text)
        except Exception:
            logger.warning("planner_llm_response_unavailable", exc_info=True)
            self.metrics["error"] = "response_unavailable"
            return None

    async def close(self):
        if self.client:
            await self.client.close()


def deterministic_strategy(world) -> TripStrategy:
    inp = world.input
    poi_map = {p.id: p for p in inp.pois}
    bases = world.base_stop_ids
    # Circuit stops are visited in their Research order; genuine bases get a balanced day allocation.
    [s.id for s in inp.stops if s.type.casefold() in {"circuit_stop", "mandatory_poi"}]
    # A circuit can have no researched overnight base. The user-provided origin/end is a valid
    # planning anchor, but circuit temples must never be presented as hotel bases.
    mandatory = [poi_map[i] for i in world.mandatory_poi_ids]
    circuit = any(
        req.requirement_type == "all_route_members" for req in inp.requirements
    )
    if circuit:
        # Preserve Research's circuit order, partition it once, and use approximate overnight
        # areas near the final route member rather than falsely selecting lodging.
        ordered = [
            poi_map[stop.id]
            for stop in inp.stops
            if stop.id in poi_map and poi_map[stop.id].is_mandatory
        ]
        size = (len(ordered) + inp.duration_days - 1) // inp.duration_days
        chunks = [ordered[i : i + size] for i in range(0, len(ordered), size)]
        while len(chunks) < inp.duration_days:
            chunks.append([])
        days = []
        for n, chunk in enumerate(chunks, 1):
            anchor = (
                f"overnight:{chunk[-1].id}"
                if chunk and n < inp.duration_days
                else "end_anchor"
            )
            days.append(
                DaySkeleton(
                    day_number=n,
                    base_stop_id=anchor,
                    theme="Route day",
                    kind=DayKind.ROUTE,
                    candidate_poi_ids=[x.id for x in chunk],
                )
            )
        return TripStrategy(
            base_sequence=[],
            nights_by_base={
                f"overnight:{chunk[-1].id}": 1 for chunk in chunks[:-1] if chunk
            },
            days=days,
            mandatory_assignments={req.id: 1 for req in inp.requirements},
            rationale="Circuit partition follows researched route-member order with approximate overnight areas near each day’s final route member.",
        )
    # Bases are selected only through typed Research IDs.  Older reports may
    # not map destination requirements to stops; then preserve the supplied
    # base candidates for the LLM/deterministic gate rather than comparing
    # display names.
    required_requirement_ids = {
        req.id
        for req in getattr(inp, "requirements", [])
        if req.required and req.requirement_type == "destination_or_base"
    }
    required_bases = [
        stop.id
        for stop in inp.stops
        if set(stop.requirement_ids) & required_requirement_ids
    ]
    # In the absence of typed base mappings, minimise hotel/base changes.  A
    # discovered optional base is not an instruction to stay there.  The LLM
    # may still propose another supplied base, but the safe fallback is one.
    explicitly_requested_bases = (
        bases if getattr(inp, "destination_type", None) == "cities" else []
    )
    base_sequence = (
        required_bases or explicitly_requested_bases or bases[:2] or ["origin_base"]
    )
    days = []
    for n in range(1, inp.duration_days + 1):
        base = (
            base_sequence[
                min(
                    len(base_sequence) - 1,
                    (n - 1) * len(base_sequence) // max(1, inp.duration_days),
                )
            ]
            if base_sequence
            else None
        )
        days.append(
            DaySkeleton(
                day_number=n,
                base_stop_id=base,
                theme="Grounded local exploration",
                kind=DayKind.SIGHTSEEING,
                candidate_poi_ids=[],
            )
        )
    # Put mandatory POIs on their matching planning context before optional POIs.
    for index, poi in enumerate(mandatory):
        candidate = [d for d in days if compatible_with_day(poi, d, world)] or [
            days[min(index, len(days) - 1)]
        ]
        candidate[0].candidate_poi_ids.append(poi.id)
    optional = [poi_map[i] for i in world.optional_poi_ids]
    for poi in optional:
        candidate = [d for d in days if compatible_with_day(poi, d, world)] or days
        min(candidate, key=lambda d: len(d.candidate_poi_ids)).candidate_poi_ids.append(
            poi.id
        )
    counts = Counter(d.base_stop_id for d in days if d.base_stop_id)
    assignments = {
        req.id: next(
            (
                d.day_number
                for d in days
                if any(
                    req.id in poi_map[p].requirement_ids for p in d.candidate_poi_ids
                )
            ),
            1,
        )
        for req in inp.requirements
    }
    return TripStrategy(
        base_sequence=base_sequence,
        nights_by_base=dict(counts),
        days=days,
        mandatory_assignments=assignments,
        rationale="Deterministic grounded baseline; LLM may refine only supplied IDs.",
    )


def enforce_mandatory(strategy: TripStrategy, world) -> TripStrategy:
    """An LLM strategy has no authority to discard a user promise."""
    included = {poi_id for day in strategy.days for poi_id in day.candidate_poi_ids}
    for index, poi_id in enumerate(world.mandatory_poi_ids):
        if poi_id not in included:
            strategy.days[index % len(strategy.days)].candidate_poi_ids.append(poi_id)
    return strategy


def balance_optional_days(strategy: TripStrategy, world) -> TripStrategy:
    """Avoid empty sightseeing days when a compatible researched optional POI exists."""
    poi_map = {poi.id: poi for poi in world.input.pois}
    for day in strategy.days:
        if day.candidate_poi_ids:
            continue
        candidates = [
            poi_id
            for poi_id in world.optional_poi_ids
            if compatible_with_day(poi_map[poi_id], day, world)
        ]
        donor = next(
            (
                other
                for other in strategy.days
                if len(other.candidate_poi_ids) > 2
                and any(poi_id in candidates for poi_id in other.candidate_poi_ids)
            ),
            None,
        )
        if donor:
            poi_id = next(
                poi_id for poi_id in donor.candidate_poi_ids if poi_id in candidates
            )
            donor.candidate_poi_ids.remove(poi_id)
            day.candidate_poi_ids.append(poi_id)
    return strategy


def enforce_day_stop_compatibility(strategy: TripStrategy, world) -> TripStrategy:
    """Optional POIs cannot be silently scheduled in a different city/base."""
    poi_map = {poi.id: poi for poi in world.input.pois}
    for day in strategy.days:
        for poi_id in list(day.candidate_poi_ids):
            poi = poi_map[poi_id]
            # The explicit mandatory excursion is preserved; other optional POIs
            # stay with their typed Research stop/base.
            if compatible_with_day(poi, day, world) or poi.is_mandatory:
                continue
            target = next(
                (
                    candidate
                    for candidate in strategy.days
                    if compatible_with_day(poi, candidate, world)
                ),
                None,
            )
            if target:
                day.candidate_poi_ids.remove(poi_id)
                target.candidate_poi_ids.append(poi_id)
            else:
                # An optional candidate never creates a new base or an ungrounded
                # cross-city excursion.  It remains available in Research, but is
                # deliberately not selected by this itinerary.
                day.candidate_poi_ids.remove(poi_id)
    return strategy


def normalize_allocation(strategy: TripStrategy, world) -> TripStrategy:
    """Step-4 deterministic gate: Research IDs only, mandatory first, no duplicates."""
    poi_map = {poi.id: poi for poi in world.input.pois}
    allowed = set(poi_map)
    # Do not let an LLM or an earlier allocation leave unknown/duplicate IDs.
    seen = set()
    for day in strategy.days:
        day.candidate_poi_ids = [
            poi_id
            for poi_id in day.candidate_poi_ids
            if poi_id in allowed and not (poi_id in seen or seen.add(poi_id))
        ]
    assigned = {poi_id for day in strategy.days for poi_id in day.candidate_poi_ids}
    # Recover every typed mandatory POI onto its compatible base day when one
    # exists; otherwise retain it on the least-loaded day for the scheduler to
    # report honestly as an excursion/unschedulable requirement.
    for poi_id in world.mandatory_poi_ids:
        if poi_id in assigned:
            continue
        poi = poi_map[poi_id]
        choices = [day for day in strategy.days if compatible_with_day(poi, day, world)]
        target = min(
            choices or strategy.days, key=lambda day: len(day.candidate_poi_ids)
        )
        target.candidate_poi_ids.append(poi_id)
        assigned.add(poi_id)
    strategy = enforce_day_stop_compatibility(strategy, world)
    # Mandatory order is preserved; optional order is a transparent soft score.
    for day in strategy.days:
        mandatory = [
            poi_id
            for poi_id in day.candidate_poi_ids
            if poi_id in world.mandatory_poi_ids
        ]
        optional = [
            poi_id
            for poi_id in day.candidate_poi_ids
            if poi_id in world.optional_poi_ids
        ]
        optional.sort(
            key=lambda poi_id: optional_score(
                poi_map[poi_id], day, getattr(world.input, "activity_interests", [])
            )[0],
            reverse=True,
        )
        day.candidate_poi_ids = [*mandatory, *optional]
    return strategy


def _category_family(value: str) -> str:
    """Small deterministic affinity layer over user and Research labels."""
    value = value.casefold().replace("_", " ").replace("-", " ")
    aliases = {
        "beaches": "beach",
        "coast": "beach",
        "cuisine": "food",
        "local food": "food",
        "heritage": "culture",
        "historical": "culture",
        "shrine": "religious",
        "temple": "religious",
        "garden": "nature",
        "park": "nature",
    }
    for token, family in {
        "beach": "beach",
        "coast": "beach",
        "food": "food",
        "cuisine": "food",
        "heritage": "culture",
        "culture": "culture",
        "histor": "culture",
        "temple": "religious",
        "shrine": "religious",
        "nature": "nature",
        "garden": "nature",
        "park": "nature",
    }.items():
        if token in value:
            return family
    return aliases.get(value, value)


def optional_score(
    poi,
    day,
    interests: list[str],
    selected_categories: set[str] | None = None,
    locally_compatible: bool | None = None,
    same_cluster: bool = False,
) -> tuple[int, list[str]]:
    """Small transparent ranking signal; feasibility remains the scheduler's job."""
    categories = {
        _category_family(x) for x in [*poi.categories, *poi.normalized_categories]
    }
    matches = categories & {_category_family(x) for x in interests}
    local = (
        poi.stop_id == day.base_stop_id
        if locally_compatible is None
        else locally_compatible
    )
    score = 20 + (30 if local else -100) + 10 * len(matches)
    reasons = [
        (
            "same structured stop"
            if poi.stop_id == day.base_stop_id
            else "local coordinate attachment"
            if local
            else "different structured stop"
        )
    ]
    if matches:
        reasons.append("user-interest category match")
    if poi.priority.casefold() in {"high", "must_see"}:
        score += 10
        reasons.append("research priority")
    if same_cluster:
        score += 8
        reasons.append("same Research cluster")
    # Diversity is a soft tie-breaker only.  It uses Research categories and
    # never removes a good candidate merely to meet an artificial quota.
    if (
        selected_categories is not None
        and categories
        and not categories & selected_categories
    ):
        score += 5
        reasons.append("adds category diversity")
    return score, reasons


def validate_allocation(strategy: TripStrategy, world) -> list[ValidationIssue]:
    """Step-4 pre-scheduler validator using stable Research IDs only."""
    allowed = {poi.id for poi in world.input.pois}
    chosen = [poi_id for day in strategy.days for poi_id in day.candidate_poi_ids]
    issues = []
    for poi_id in chosen:
        if poi_id not in allowed:
            issues.append(
                ValidationIssue(
                    code="ALLOCATION_OUTSIDE_RESEARCH",
                    severity="hard",
                    message=f"Unknown selected POI ID: {poi_id}",
                )
            )
    for poi_id in world.mandatory_poi_ids:
        if poi_id not in chosen:
            issues.append(
                ValidationIssue(
                    code="MANDATORY_POI_UNALLOCATED",
                    severity="hard",
                    message=f"Mandatory POI unallocated: {poi_id}",
                )
            )
    for poi_id in set(chosen):
        if chosen.count(poi_id) > 1:
            issues.append(
                ValidationIssue(
                    code="DUPLICATE_POI_ALLOCATION",
                    severity="hard",
                    message=f"POI allocated more than once: {poi_id}",
                )
            )
    {poi.id: poi for poi in world.input.pois}
    for req in getattr(world.input, "requirements", []):
        if not req.required or req.requirement_type == "destination_or_base":
            continue
        mapped = {poi.id for poi in world.input.pois if req.id in poi.requirement_ids}
        if req.requirement_type == "all_route_members":
            # Older Research reports mark route members with is_mandatory but
            # omit per-POI requirement_ids.  The typed all-route requirement
            # therefore means every mandatory route-member candidate, never a
            # name/text heuristic.
            required_members = mapped or set(world.mandatory_poi_ids)
        else:
            required_members = mapped
        if not required_members or not required_members.issubset(set(chosen)):
            issues.append(
                ValidationIssue(
                    code="TYPED_MANDATORY_REQUIREMENT_UNALLOCATED",
                    severity="hard",
                    message=f"Typed mandatory requirement is not fully allocated: {req.id}",
                )
            )
    return issues


async def strategy_agent(world, llm: LLM) -> TripStrategy:
    baseline = deterministic_strategy(world)
    inp = world.input
    # A typed circuit is frozen Step-1–3 structure.  Step 4 may select ordinary
    # POIs, but it must not let a qualitative LLM response rewrite the route
    # partition or discard a route member.
    if any(
        req.required and req.requirement_type == "all_route_members"
        for req in inp.requirements
    ):
        return normalize_allocation(baseline, world)
    response = await llm.json(
        "You are a travel strategy planner. Improve the baseline only by selecting provided IDs. Do not create timestamps, facts, POIs, or routes. Keep the existing base sequence unless a user requirement or a mandatory POI makes another supplied base necessary; do not add a Research-discovered optional base merely to use its POIs. Treat [HARD] trip context notes as non-negotiable where the supplied data allows. Treat [SOFT] notes and traveler context as preferences only. If a note cannot be met from supplied data, do not invent a solution; record it briefly in uncertainty_notes. Return base_sequence, nights_by_base, days [{day_number,base_stop_id,theme,candidate_poi_ids}], mandatory_assignments, rationale, uncertainty_notes.",
        {
            "trip": {
                "days": inp.duration_days,
                "interests": inp.activity_interests,
                "avoid": inp.avoid,
                "travellers": inp.travellers,
                "trip_notes": inp.trip_notes,
                "trip_context_notes": inp.trip_context_notes,
                "traveler_context": inp.traveler_context,
            },
            "bases": [s.model_dump() for s in inp.stops if s.id in world.base_stop_ids],
            "pois": [
                {
                    "id": p.id,
                    "name": p.name,
                    "stop_id": p.stop_id,
                    "mandatory": p.is_mandatory,
                    "categories": p.normalized_categories or p.categories,
                }
                for p in inp.pois
            ],
            "requirements": [x.model_dump() for x in inp.requirements],
            "baseline": baseline.model_dump(),
        },
    )
    if not response:
        return normalize_allocation(baseline, world)
    try:
        value = TripStrategy.model_validate(response)
        allowed = {p.id for p in inp.pois}
        required_base_ids = {
            stop.id
            for stop in inp.stops
            if set(stop.requirement_ids)
            & {
                req.id
                for req in inp.requirements
                if req.required and req.requirement_type == "destination_or_base"
            }
        }
        allowed_bases = required_base_ids or set(world.base_stop_ids) or {"origin_base"}
        circuit = any(
            req.requirement_type == "all_route_members" for req in inp.requirements
        )

        def valid_anchor(value):
            return value in allowed_bases or (
                circuit
                and isinstance(value, str)
                and (value.startswith("overnight:") or value == "end_anchor")
            )

        if (
            len(value.days) != inp.duration_days
            or not set(value.base_sequence) <= allowed_bases
            or any(
                not set(d.candidate_poi_ids) <= allowed
                or (d.base_stop_id and not valid_anchor(d.base_stop_id))
                for d in value.days
            )
        ):
            return baseline
        return normalize_allocation(balance_optional_days(value, world), world)
    except Exception:
        logger.warning("planner_strategy_unavailable", exc_info=True)
        return normalize_allocation(baseline, world)


async def feasibility_agent(
    strategy: TripStrategy, world, llm: LLM
) -> list[dict[str, Any]]:
    """One bounded LLM turn: it may move optional POIs, never fabricate schedule facts."""
    response = await llm.json(
        "You are a feasibility planning agent. Choose at most two optional-POI moves that may improve geographic grouping or balance. Deterministic tools will calculate order, routes, hours, durations, and timestamps afterwards. Return {actions:[{poi_id,from_day,to_day,rationale}]}. Use only supplied optional IDs and 1-based existing day numbers; do not drop mandatory POIs or invent travel facts.",
        {
            "days": [day.model_dump() for day in strategy.days],
            "optional_poi_ids": world.optional_poi_ids,
            "route_facts": [route.model_dump() for route in world.input.routes],
        },
    )
    actions = []
    raw_actions = response.get("actions", []) if isinstance(response, dict) else []
    if not isinstance(raw_actions, list):
        return actions
    for raw in raw_actions[:2]:
        if not isinstance(raw, dict):
            continue
        poi_id, from_day, to_day = (
            raw.get("poi_id"),
            raw.get("from_day"),
            raw.get("to_day"),
        )
        if (
            poi_id in world.optional_poi_ids
            and isinstance(from_day, int)
            and isinstance(to_day, int)
            and 1 <= from_day <= len(strategy.days)
            and 1 <= to_day <= len(strategy.days)
        ):
            actions.append(
                {
                    "poi_id": poi_id,
                    "from_day": from_day,
                    "to_day": to_day,
                    "rationale": str(raw.get("rationale", ""))[:300],
                }
            )
    return actions


async def reviewer_agent(
    result, world, llm: LLM
) -> tuple[list[ReviewerFinding], list[ReviewerEdit]]:
    """One bounded qualitative pass over final Step-4 output, never raw research."""
    poi_map = {poi.id: poi for poi in world.input.pois}
    days = []
    for day in result.days:
        activities = []
        for activity in day.activities:
            poi = poi_map.get(activity.poi_id)
            activities.append(
                {
                    "id": activity.poi_id,
                    "name": activity.name,
                    "mandatory": activity.mandatory,
                    "categories": (poi.normalized_categories or poi.categories)
                    if poi
                    else [],
                    "start": activity.start_time,
                    "end": activity.end_time,
                    "visit_minutes": activity.planned_visit_minutes,
                    "restrictions": (poi.restrictions if poi else []),
                    "accessibility": (poi.accessibility_notes if poi else []),
                }
            )
        days.append(
            {
                "day_number": day.day_number,
                "date": day.date,
                "base": day.overnight_base,
                "kind": day.day_kind.value,
                "theme": day.title,
                "schedule_state": day.schedule_state,
                "activities": activities,
                "known_travel_minutes": sum(
                    leg.duration_minutes or 0 for leg in day.transport_legs
                ),
                "activity_minutes": sum(
                    item.planned_visit_minutes for item in day.activities
                ),
                "meals_rest": day.meals_rest,
                "warnings": day.warnings,
                "confirm": day.confirm_before_visit,
            }
        )
    payload = {
        "trip": {
            "origin": world.input.origin,
            "end": (world.input.stops[-1].name if world.input.stops else None),
            "dates": [world.input.start_date, world.input.end_date],
            "travellers": world.input.travellers,
            "interests": world.input.activity_interests,
            "avoid": world.input.avoid,
            "accessibility": world.input.travellers.get("accessibility_needs", []),
                # getattr keeps the reviewer compatible with older serialized
                # planner inputs while current PlannerInput always supplies them.
                "trip_notes": getattr(world.input, "trip_notes", []),
                "trip_context_notes": getattr(world.input, "trip_context_notes", []),
                "traveler_context": getattr(world.input, "traveler_context", {}),
            "mandatory_requirements": [
                r.requirement_text for r in world.input.requirements if r.required
            ],
        },
        "days": days,
        "optional_poi_ids": world.optional_poi_ids,
        "mandatory_poi_ids": world.mandatory_poi_ids,
        "unscheduled_optional": result.unscheduled_optional_pois,
        "warnings": result.warnings,
        "uncertainties": result.uncertainties,
    }
    response = await llm.json(
        "You are the single final human-quality trip reviewer. Return {findings:[...],edits:[...]}. Findings must be concise factual observations only; zero findings and zero edits is good for a good plan. At most two edits. Allowed edit actions ONLY: MOVE_OPTIONAL_POI, DROP_OPTIONAL_POI, SWAP_POI_ORDER, SET_DAY_THEME. Use only supplied IDs. Never add POIs, bases, route facts, timings, or opening hours. Never alter a mandatory POI. Required edit fields: edit_id, finding_id, action, rationale; use day and POI fields only when relevant.",
        payload,
    )
    findings = []
    finding_ids = set()
    for raw in (response or {}).get("findings", []):
        try:
            finding = ReviewerFinding.model_validate(raw)
            if finding.finding_id not in finding_ids:
                findings.append(finding)
                finding_ids.add(finding.finding_id)
        except ValueError:
            logger.debug("reviewer_finding_rejected")
    edits = []
    for raw in (response or {}).get("edits", [])[:2]:
        try:
            edit = ReviewerEdit.model_validate(raw)
            # An edit must have a real finding and can never refer to a mandatory POI.
            poi_allowed = (
                edit.action == "SET_DAY_THEME" or edit.poi_id in world.optional_poi_ids
            )
            if (
                edit.finding_id in finding_ids
                and poi_allowed
                and len(edit.rationale) <= 300
            ):
                edits.append(edit)
        except ValueError:
            logger.debug("reviewer_edit_rejected")
    return findings, edits
