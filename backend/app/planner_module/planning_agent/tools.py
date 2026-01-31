"""Deterministic tool surface used by the single Planning Agent."""

from __future__ import annotations

import re
from datetime import date, timedelta
from time import monotonic
from collections import Counter
from typing import Any

from langchain_core.tools import StructuredTool

from ..adapters import build_world
from ..agents import compatible_with_day, deterministic_strategy, normalize_allocation, validate_allocation
from ..models.planning import (
    BudgetSummary,
    DayPlan,
    DayRouteChain,
    PlanningResult,
    TripRoutePlan,
    ValidationIssue,
)
from ..tools.route_completeness import build_route_plan
from ..tools.routing import RouteResolver
from ..tools.scheduling import _best_order
from ..validation import final_validation, has_day_content
from .models import HardConstraints, PlanningCheckpoint, PlanningNotebook, PlanningObservation, PlanningSubmission, SemanticDayDraft


_DRIVING_MODES = {"driving", "drive", "taxi", "rental_car"}


def maximum_driving_issues(days, maximum_minutes: int | None) -> list[ValidationIssue]:
    """Return hard issues only when a stated daily driving constraint is exceeded."""
    if maximum_minutes is None:
        return []
    issues = []
    for day in days:
        driving = sum(
            leg.duration_minutes or 0
            for leg in day.transport_legs
            if str(leg.mode).lower() in _DRIVING_MODES
        )
        if driving > maximum_minutes:
            issues.append(
                ValidationIssue(
                    code="MAX_DRIVING_EXCEEDED",
                    severity="hard",
                    day_number=day.day_number,
                    message=(
                        f"Driving exceeds the {maximum_minutes}-minute daily maximum."
                    ),
                )
            )
    return issues


class PlanningToolRuntime:
    """Small mutable notebook plus references to deterministic planning results."""

    def __init__(self) -> None:
        self.world = None
        self.settings = None
        self.notebook = PlanningNotebook()
        self.observations: list[PlanningObservation] = []
        self.artifacts: dict[str, Any] = {}
        self.route_cache: dict = {}
        self.submitted = False
        self.candidate_result = None
        self.last_validation: dict[str, Any] | None = None
        self.tool_calls: Counter[str] = Counter()
        self.checkpoint_count = 0
        self.submission_count = 0
        self.day_simulation_count = 0
        self.route_optimizer_count = 0
        self.budget_calculation_count = 0
        self.validation_count = 0
        self.materialization_count = 0
        self.route_batch_count = 0
        self.completion_path: str | None = None
        self.fallback_reason: str | None = None
        self.last_valid_plan_version: int | None = None
        self.termination_reason: str | None = None
        self.started_at: float | None = None
        self.pre_repair_candidate = None
        self.pre_repair_notebook = None

    def reset(self, world, settings) -> None:
        self.world, self.settings = world, settings
        self.notebook = PlanningNotebook()
        self.observations, self.artifacts, self.route_cache = [], {}, {}
        self.submitted = False
        self.candidate_result = self.last_validation = None
        self.tool_calls.clear()
        self.checkpoint_count = self.submission_count = 0
        self.day_simulation_count = self.route_optimizer_count = 0
        self.budget_calculation_count = self.validation_count = 0
        self.materialization_count = self.route_batch_count = 0
        self.completion_path = self.fallback_reason = self.termination_reason = None
        self.last_valid_plan_version = None
        self.started_at = monotonic()
        self.pre_repair_candidate = self.pre_repair_notebook = None

    def remaining_runtime_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        return self.settings.planning_agent_runtime_seconds - (monotonic() - self.started_at)

    def _observe(self, tool: str, status: str, summary: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        self.tool_calls[tool] += 1
        item = PlanningObservation(id=f"planning:{tool}:{len(self.observations) + 1}", tool=tool, status=status, summary=summary, data=data or {})
        self.observations.append(item)
        return {"status": status, "observation_ref": item.id, "summary": summary, **(data or {})}

    def _constraints(self) -> HardConstraints:
        raw = [*self.world.input.mandatory_constraints, *self.world.input.trip_context_notes]
        max_driving = None
        for value in raw:
            normalized = " ".join(value.casefold().split())
            match = re.search(
                r"(?:do not plan (?:any )?road travel longer than|keep driving to (?:a )?maximum of|limit driving to no more than|max(?:imum)?|no)\s+(\d+(?:\.\d+)?)\s*hours?(?:\s+of)?\s+(?:driv(?:ing|e)|road travel)?(?:.*?(?:per|a)\s*day)?",
                normalized,
            )
            if match:
                max_driving = round(float(match.group(1)) * 60)
                break
            match = re.search(r"(?:max(?:imum)?|no)\s+(\d+)\s*minutes?(?:\s+of)?\s+(?:driv(?:ing|e)|road travel)(?:.*?(?:per|a)\s*day)?", normalized)
            if match:
                max_driving = int(match.group(1))
                break
        return HardConstraints(
            max_driving_minutes_per_day=max_driving,
            hard_day_end=self.world.input.time_preferences.get("hard_day_end"),
            required_accessibility=list(self.world.input.travellers.get("accessibility_needs", [])),
            raw_required_notes=raw,
        )

    @staticmethod
    def _checkpoint_issue(code: str, message: str, *, day_number: int | None = None,
                          entity_ids: list[str] | None = None,
                          requirement_ids: list[str] | None = None) -> dict[str, Any]:
        return {
            "code": code, "severity": "hard", "repairability": "semantic_plan",
            "day_number": day_number, "entity_ids": entity_ids or [],
            "requirement_ids": requirement_ids or [], "message": message,
            "suggested_actions": ["update_checkpoint"],
        }

    def _validate_checkpoint(self, checkpoint: PlanningCheckpoint) -> list[dict[str, Any]]:
        input_data = self.world.input
        allowed_pois = {poi.id for poi in input_data.pois}
        allowed_bases = set(self.world.base_stop_ids) | {"origin_anchor", "end_anchor", "origin_base"}
        selected = [poi_id for day in checkpoint.days for poi_id in day.selected_poi_ids]
        issues: list[dict[str, Any]] = []
        day_numbers = [day.day_number for day in checkpoint.days]
        if len(checkpoint.days) != input_data.duration_days or sorted(day_numbers) != list(range(1, input_data.duration_days + 1)):
            issues.append(self._checkpoint_issue("DAY_COVERAGE", "A checkpoint must contain exactly one semantic draft for every trip day."))
        if not set(selected) <= allowed_pois:
            issues.append(self._checkpoint_issue("UNKNOWN_POI", "Checkpoint contains a POI ID outside ResearchReport.", entity_ids=sorted(set(selected) - allowed_pois)))
        if len(selected) != len(set(selected)):
            duplicates = [item for item, count in Counter(selected).items() if count > 1]
            issues.append(self._checkpoint_issue("DUPLICATE_POI", "Checkpoint assigns a POI more than once.", entity_ids=duplicates))
        bad_bases = [day.base_stop_id for day in checkpoint.days if day.base_stop_id and not (day.base_stop_id in allowed_bases or day.base_stop_id.startswith("overnight:"))]
        if bad_bases:
            issues.append(self._checkpoint_issue("UNKNOWN_BASE", "Checkpoint contains an unresearched base ID.", entity_ids=bad_bases))
        if any(day.base_stop_id and day.base_stop_id.startswith("overnight:") and day.base_stop_id.split(":", 1)[1] not in allowed_pois for day in checkpoint.days):
            issues.append(self._checkpoint_issue("UNKNOWN_OVERNIGHT", "Overnight circuit areas must reference a researched POI."))
        required_pois = set(self.world.mandatory_poi_ids)
        missing_pois = sorted(required_pois - set(selected))
        if missing_pois:
            requirement_ids = sorted({req for poi in input_data.pois if poi.id in missing_pois for req in poi.requirement_ids})
            issues.append(self._checkpoint_issue("MANDATORY_POI_UNSCHEDULED", "Every mandatory researched POI must be allocated before routing.", entity_ids=missing_pois, requirement_ids=requirement_ids))
        expected = list(self.world.base_stop_ids)
        if input_data.destination_type in {"cities", "route"} and expected and checkpoint.base_sequence != expected:
            issues.append(self._checkpoint_issue("BASE_ORDER", "Confirmed primary-base order must be preserved.", entity_ids=expected))
        if checkpoint.nights_by_base and any(base not in allowed_bases and not base.startswith("overnight:") for base in checkpoint.nights_by_base):
            issues.append(self._checkpoint_issue("UNKNOWN_NIGHT_BASE", "Nights must use a supplied base or an explicit overnight anchor."))
        if checkpoint.nights_by_base and sum(checkpoint.nights_by_base.values()) > input_data.duration_days:
            issues.append(self._checkpoint_issue("NIGHT_COUNT", "Night count cannot exceed the trip duration."))
        stay_names = {str(item.get("name")) for item in input_data.stay_bases if item.get("name")}
        invalid_stays = [value for value in checkpoint.stay_area_by_night.values() if value != "unknown" and value not in stay_names]
        if invalid_stays:
            issues.append(self._checkpoint_issue("UNKNOWN_STAY_AREA", "Stay areas must be supplied by Research or explicitly unknown.", entity_ids=invalid_stays))
        stops = {stop.id: stop for stop in input_data.stops}
        for night, name in checkpoint.stay_area_by_night.items():
            area = next((area for area in input_data.stay_bases if area.get("name") == name), None)
            day = next((day for day in checkpoint.days if day.day_number == night), None)
            base = stops.get(day.base_stop_id) if day else None
            if area and base:
                from ..models.planning import Coordinates
                coordinates = area.get("coordinates")
                if not coordinates or RouteResolver._haversine_km(base.coordinates, Coordinates.model_validate(coordinates)) > self.settings.local_attachment_km:
                    issues.append(self._checkpoint_issue("STAY_BASE_MISMATCH", "Stay area must be located near its assigned researched base.", day_number=night))
        if day_numbers != list(range(1, input_data.duration_days + 1)):
            issues.append(self._checkpoint_issue("DAY_ORDER", "Days must be in chronological order."))
        if any(day.preferred_order and (len(day.preferred_order) != len(day.selected_poi_ids) or set(day.preferred_order) != set(day.selected_poi_ids)) for day in checkpoint.days):
            issues.append(self._checkpoint_issue("POI_ORDER", "Preferred order must include exactly the selected POIs."))
        invalid_confirmation_ids = sorted({
            poi_id
            for day in checkpoint.days
            for poi_id in day.confirmation_required_poi_ids
            if poi_id not in day.selected_poi_ids
        })
        if invalid_confirmation_ids:
            issues.append(self._checkpoint_issue(
                "CONFIRMATION_POI",
                "Session-confirmation IDs must be selected on the same day.",
                entity_ids=invalid_confirmation_ids,
            ))
        poi_map = {poi.id: poi for poi in input_data.pois}
        circuit = any(req.required and req.requirement_type == "all_route_members" for req in input_data.requirements)
        for day in checkpoint.days:
            if day.start_anchor_id or day.end_anchor_id:
                issues.append(self._checkpoint_issue("ANCHORS_DERIVED", "Anchors are derived from bases, not model supplied.", day_number=day.day_number))
            if not circuit and any(poi_id in poi_map and not compatible_with_day(poi_map[poi_id], day, self.world, self.settings.local_attachment_km) for poi_id in day.selected_poi_ids):
                issues.append(self._checkpoint_issue("POI_BASE_MISMATCH", "Selected visits do not belong to this day's destination.", day_number=day.day_number))
            if day.day_kind.value == "rest_or_buffer" and not day.notes:
                issues.append(self._checkpoint_issue("REST_REASON", "Intentional rest requires an explanation.", day_number=day.day_number))
        if not circuit:
            actual_bases = [day.base_stop_id for index, day in enumerate(checkpoint.days)
                            if day.base_stop_id in self.world.base_stop_ids
                            and (index == 0 or checkpoint.days[index - 1].base_stop_id != day.base_stop_id)]
            if actual_bases != checkpoint.base_sequence or set(actual_bases) != set(self.world.base_stop_ids):
                issues.append(self._checkpoint_issue("BASE_COVERAGE", "All confirmed bases must occur in the declared order."))
            expected_nights = dict(Counter(day.base_stop_id for day in checkpoint.days[:-1] if day.base_stop_id))
            if checkpoint.nights_by_base != expected_nights:
                issues.append(self._checkpoint_issue("NIGHT_COUNT", "Nights must agree with the actual overnight day bases."))
        elif [item for item in selected if item in required_pois] != list(self.world.mandatory_poi_ids):
            issues.append(self._checkpoint_issue("CIRCUIT_ORDER", "Keep the mandatory circuit member order."))
        if any(night < 1 or night >= input_data.duration_days for night in checkpoint.stay_area_by_night):
            issues.append(self._checkpoint_issue("STAY_NIGHT", "Stay selections must refer to an actual trip night."))
        if any(count < 0 for count in checkpoint.nights_by_base.values()):
            issues.append(self._checkpoint_issue("NIGHT_COUNT", "Negative nights are invalid."))
        return issues

    @staticmethod
    def candidate_rank(candidate) -> tuple:
        if not candidate:
            return (-float("inf"), 0, 0, 0)
        result = candidate["result"]
        return (
            -sum(issue.severity == "hard" for issue in candidate["issues"]),
            sum(item.get("status") == "SCHEDULED" for item in candidate["mandatory"].values()),
            sum(day.schedule_state == "complete" and bool(day.activities or day.transport_legs) for day in result.days),
            sum(len(day.activities) for day in result.days),
        )

    def _canonical_node_id(self, value: str) -> str | None:
        """Resolve one unambiguous display label to an existing research ID."""
        if value in {"origin_anchor", "end_anchor"}:
            return value
        known = {
            item.id: item.id
            for item in [*self.world.input.stops, *self.world.input.pois]
        }
        if value in known:
            return value
        if value.casefold() == self.world.input.origin.casefold():
            return "origin_anchor"
        end_location = self.world.input.end_location or self.world.input.origin
        if (
            end_location.casefold() != self.world.input.origin.casefold()
            and value.casefold() == end_location.casefold()
        ):
            return "end_anchor"
        labels: dict[str, list[str]] = {}
        for item in [*self.world.input.stops, *self.world.input.pois]:
            labels.setdefault(item.name.casefold(), []).append(item.id)
        matches = list(dict.fromkeys(labels.get(value.casefold(), [])))
        return matches[0] if len(matches) == 1 else None

    def checkpoint_plan(self, checkpoint: PlanningCheckpoint) -> dict[str, Any]:
        """Persist semantic decisions after mechanical ID and route-order checks."""
        issues = self._validate_checkpoint(checkpoint)
        if issues:
            return self._observe("checkpoint_plan", "invalid", "Semantic checkpoint violates planner invariants.", {"issues": issues})
        self.notebook = PlanningNotebook(
            plan_version=self.notebook.plan_version + 1,
            base_sequence=checkpoint.base_sequence,
            nights_by_base=checkpoint.nights_by_base,
            stay_area_by_night=checkpoint.stay_area_by_night,
            days=checkpoint.days,
            alternatives=checkpoint.alternatives,
            latest_day_simulation_references=self.notebook.latest_day_simulation_references,
            latest_route_references=self.notebook.latest_route_references,
            latest_budget_reference=self.notebook.latest_budget_reference,
            latest_validation_reference=self.notebook.latest_validation_reference,
            unresolved_items=checkpoint.unresolved_items,
            guidance_items=checkpoint.guidance_items,
        )
        self.checkpoint_count += 1
        self.last_valid_plan_version = self.notebook.plan_version
        return self._observe("checkpoint_plan", "ok", "Semantic plan checkpointed.", {"plan_version": self.notebook.plan_version})

    def _strategy(self):
        from ..models.planning import DaySkeleton, TripStrategy

        return TripStrategy(
            base_sequence=self.notebook.base_sequence,
            nights_by_base=self.notebook.nights_by_base,
            days=[DaySkeleton(
                day_number=day.day_number,
                base_stop_id=day.base_stop_id,
                theme=day.theme,
                kind=day.day_kind,
                candidate_poi_ids=day.preferred_order or day.selected_poi_ids,
                preserve_order=bool(day.preferred_order),
                confirmation_required_poi_ids=day.confirmation_required_poi_ids,
            ) for day in self.notebook.days],
            mandatory_assignments={},
            rationale="Semantic decisions checkpointed by Planner Agent.",
            uncertainty_notes=self.notebook.unresolved_items,
        )

    async def _materialize(self):
        from ..nodes.planning import _materialize

        self.materialization_count += 1
        self.route_batch_count += 1
        state = {"world": self.world, "settings": self.settings, "route_cache": self.route_cache,
                 "stay_area_by_night": self.notebook.stay_area_by_night}
        days, issues, resolver = await _materialize(state, self._strategy())
        self.route_cache.update(resolver.cache)
        return days, issues, resolver

    async def calculate_route(self, origin_id: str, destination_id: str) -> dict[str, Any]:
        """Calculate one factual route only between supplied Research identities."""
        origin_id = self._canonical_node_id(origin_id)
        destination_id = self._canonical_node_id(destination_id)
        if not origin_id or not destination_id:
            return self._observe(
                "calculate_route",
                "invalid",
                "Unknown or ambiguous route endpoint; use a supplied stable ID.",
            )
        resolver = RouteResolver(self.world.input, local_attachment_km=self.settings.local_attachment_km)
        resolver.cache.update(self.route_cache)
        route = await resolver.resolve(origin_id, destination_id)
        self.route_cache.update(resolver.cache)
        reference = f"route:{len(self.artifacts) + 1}"
        self.artifacts[reference] = route
        self.notebook.latest_route_references.append(reference)
        return self._observe("calculate_route", "ok", "Route calculated from provider or Research evidence.", {"route_ref": reference, "route": route.model_dump(mode="json")})

    async def optimize_day_route(self, day_number: int, poi_ids: list[str], start_anchor_id: str, end_anchor_id: str) -> dict[str, Any]:
        """Order supplied POIs only; it never selects or drops a POI."""
        poi_map = {poi.id: poi for poi in self.world.input.pois}
        if not set(poi_ids) <= set(poi_map) or len(poi_ids) != len(set(poi_ids)):
            return self._observe("optimize_day_route", "invalid", "Unknown or duplicate POI ID.")
        resolver = RouteResolver(self.world.input, local_attachment_km=self.settings.local_attachment_km)
        resolver.cache.update(self.route_cache)
        ordered = await _best_order([poi_map[item] for item in poi_ids], start_anchor_id, end_anchor_id, resolver, self.world.input.start_date, 8 * 60)
        self.route_cache.update(resolver.cache)
        self.route_optimizer_count += 1
        return self._observe("optimize_day_route", "ok", "Deterministic geographic order calculated.", {"ordered_poi_ids": [poi.id for poi in ordered]})

    async def simulate_day(self, day_number: int) -> dict[str, Any]:
        """Build an exact deterministic timeline for one already checkpointed day."""
        if not any(day.day_number == day_number for day in self.notebook.days):
            return self._observe("simulate_day", "invalid", "Day has not been checkpointed.")
        days, issues, _ = await self._materialize()
        day = next(item for item in days if item.day_number == day_number)
        reference = f"simulation:{day_number}:{self.notebook.plan_version}"
        self.artifacts[reference] = day
        self.notebook.latest_day_simulation_references[day_number] = reference
        self.day_simulation_count += 1
        return self._observe("simulate_day", "ok", "Deterministic day timeline calculated.", {"simulation_ref": reference, "schedule_state": day.schedule_state, "issue_codes": [item.code for item in issues if item.day_number == day_number]})

    def calculate_budget(self, scheduled_ids: set[str] | None = None) -> dict[str, Any]:
        """Calculate only evidence-backed arithmetic; unknown categories remain unknown."""
        data = self.world.input
        if scheduled_ids is None:
            scheduled_ids = {activity.poi_id for day in self.last_validation["result"].days for activity in day.activities} if self.last_validation else set()
        adults = int(data.travellers.get("num_adults", 1) or 0)
        seniors = int(data.travellers.get("num_seniors", 0) or 0)
        children = int(data.travellers.get("num_children", 0) or 0)
        activity_total = 0.0
        unknown = False
        for poi in data.pois:
            if poi.id not in scheduled_ids:
                continue
            if poi.adult_entry_cost is None or (poi.entry_currency or "").upper() != data.currency.upper():
                unknown = True
                continue
            activity_total += poi.adult_entry_cost * adults
            unknown = unknown or bool(seniors or children)
        categories = {
            "activities": {"status": "partial" if unknown else "estimated", "amount": activity_total,
                           "currency": data.currency, "basis": "Known adult entry prices for scheduled visits only; price verification and other traveller fares require confirmation."},
            "long_distance_transport": {"status": "unknown", "amount": None},
            "local_transport": {"status": "unknown", "amount": None},
            "accommodation": {"status": "unknown", "amount": None},
            "food_daily_spend": {"status": "unknown", "amount": None},
            "other_reserve": {"status": "unknown", "amount": None},
        }
        reference = f"budget:{self.notebook.plan_version}"
        self.artifacts[reference] = categories
        self.notebook.latest_budget_reference = reference
        self.budget_calculation_count += 1
        return self._observe("calculate_budget", "ok", "Budget arithmetic calculated; unsupported categories remain unknown.", {"budget_ref": reference, "categories": categories})

    def _budget_summary(self, categories) -> BudgetSummary:
        verified = sum(value.get("amount") or 0 for value in categories.values() if value["status"] == "verified")
        estimated = sum(value.get("amount") or 0 for value in categories.values() if value["status"] in {"estimated", "partial"})
        unknown = [key for key, value in categories.items() if value["status"] in {"unknown", "partial"}]
        limit = self.world.input.budget.get("total")
        notes = ["Budget fit is unconfirmed while major costs or traveller fares are unknown."] if unknown else []
        if isinstance(limit, (int, float)) and verified + estimated > limit:
            notes.append("The available cost subtotal already exceeds the trip budget.")
        return BudgetSummary(
            known_attraction_cost=categories.get("activities", {}).get("amount") or 0,
            currency=self.world.input.currency, status="partial" if verified or estimated else "unknown",
            categories=categories, total_verified=verified, total_estimated=estimated,
            unknown_categories=unknown, notes=notes,
        )

    async def _compile_result(self, *, include_budget: bool = True):
        days, issues, resolver = await self._materialize()
        for day, semantic in zip(days, self.notebook.days, strict=True):
            day.warnings.extend(semantic.notes)
            if not self.notebook.stay_area_by_night.get(day.day_number) or self.notebook.stay_area_by_night.get(day.day_number) == "unknown":
                day.confirm_before_visit.append("City-level base only; select accommodation and verify local access before travel.")
        strategy = self._strategy()
        issues = [*validate_allocation(strategy, self.world), *issues]
        issues.extend(
            ValidationIssue(
                code="EMPTY_SIGHTSEEING_DAY", severity="hard", day_number=day.day_number,
                message="No visit or travel remains on this day; choose a feasible researched alternative or explain an intentional rest day.",
            )
            for day in days if day.day_kind.value != "rest_or_buffer" and not has_day_content(day)
        )
        checkpoint = PlanningCheckpoint(**self.notebook.model_dump(include=set(PlanningCheckpoint.model_fields)))
        issues.extend(ValidationIssue(**{key: value for key, value in item.items() if key in ValidationIssue.model_fields}) for item in self._validate_checkpoint(checkpoint))
        route_plan, route_issues = await build_route_plan(days, resolver, self.world.input)
        issues.extend(route_issues)
        scheduled = {activity.poi_id for day in days for activity in day.activities}
        mandatory_allocation = {}
        for poi_id in self.world.mandatory_poi_ids:
            scheduled_day = next(
                (day.day_number for day in days if poi_id in {item.poi_id for item in day.activities}),
                None,
            )
            assigned_day = next(
                (day.day_number for day in strategy.days if poi_id in day.candidate_poi_ids),
                None,
            )
            if scheduled_day is not None:
                mandatory_allocation[poi_id] = {
                    "status": "SCHEDULED", "day_number": scheduled_day,
                }
            elif assigned_day is not None:
                # Research grounded the place and the skeleton reserved it; an
                # unavailable route/timing fact must become a visible draft
                # dependency, never a silently discarded must-see.
                mandatory_allocation[poi_id] = {
                    "status": "ALLOCATED_UNRESOLVED", "day_number": assigned_day,
                    "reason": "Exact route or timing evidence is unavailable; confirm this reserved visit before travel.",
                }
                day = next(item for item in days if item.day_number == assigned_day)
                day.confirm_before_visit.append(
                    f"{next(poi.name for poi in self.world.input.pois if poi.id == poi_id)} is reserved but needs route/timing confirmation."
                )
            else:
                mandatory_allocation[poi_id] = {"status": "UNALLOCATED", "day_number": None}
        constraints = self._constraints()
        issues.extend(
            maximum_driving_issues(days, constraints.max_driving_minutes_per_day)
        )
        if any(term in " ".join(constraints.required_accessibility).casefold()
               for term in ("wheelchair", "step-free", "step free")):
            # Narrative POI notes cannot certify the accessibility of the
            # complete transport-and-visit chain. Do not silently treat them as proof.
            issues.append(ValidationIssue(
                code="ACCESSIBILITY_CONFIRMATION_REQUIRED", severity="hard",
                message="Confirm the requested accessibility for both selected visits and transport; this planner cannot certify it from narrative research notes alone.",
            ))
            for day in days:
                day.confirm_before_visit.append("Accessibility of the full daily route requires confirmation before travel.")
        satisfied, unsatisfied = [], []
        for requirement in self.world.input.requirements:
            mapped = {poi.id for poi in self.world.input.pois if requirement.id in poi.requirement_ids}
            if not requirement.required:
                continue
            if requirement.requirement_type == "destination_or_base":
                required_bases = {stop.id for stop in self.world.input.stops if requirement.id in stop.requirement_ids}
                covered = bool(required_bases) and required_bases.issubset({day.base_stop_id for day in strategy.days})
            else:
                required_pois = mapped or (set(self.world.mandatory_poi_ids) if requirement.requirement_type == "all_route_members" else set())
                covered = bool(required_pois) and required_pois.issubset(scheduled)
            (satisfied if covered else unsatisfied).append(requirement.id)
        budget_categories = (
            self.calculate_budget(scheduled)["categories"]
            if include_budget
            else {"activities": {"status": "unknown", "amount": None}}
        )
        result = PlanningResult(
            status="complete" if not any(item.severity == "hard" for item in issues) and not unsatisfied else "best_effort",
            trip_summary=f"{self.world.input.trip_name} — {self.world.input.duration_days}-Day Itinerary",
            selected_bases=[next((stop.name for stop in self.world.input.stops if stop.id == item), item) for item in strategy.base_sequence],
            nights_by_base=strategy.nights_by_base, days=days,
            unscheduled_optional_pois=[poi_id for poi_id in self.world.optional_poi_ids if poi_id not in scheduled],
            satisfied_mandatory_requirements=satisfied, unsatisfied_mandatory_requirements=unsatisfied,
            assumptions=self.world.input.assumptions,
            uncertainties=[
                *self.world.input.unresolved_gaps,
                *self.notebook.unresolved_items,
                *[
                    f"Mandatory visit {poi_id} remains reserved with unresolved timing."
                    for poi_id, allocation in mandatory_allocation.items()
                    if allocation["status"] == "ALLOCATED_UNRESOLVED"
                ],
            ],
            warnings=[item.message for item in issues if item.severity != "hard"],
            budget_summary=self._budget_summary(budget_categories),
            planning_metrics={"planner_agent": True, "scheduled_pois": len(scheduled), "validation_issues": len(issues), "unknown_route_legs": sum(1 for day in days for leg in day.transport_legs if leg.uncertainty), **route_plan.metrics},
            trace_references={},
        )
        validation = final_validation(result, self.world, route_plan, issues, mandatory_allocation)
        return result, issues, route_plan, mandatory_allocation, validation

    async def validate_trip(self, *, include_budget: bool = True) -> dict[str, Any]:
        """Run deterministic schedule, route, budget, and hard-constraint checks."""
        result, issues, route_plan, mandatory, validation = await self._compile_result(
            include_budget=include_budget
        )
        reference = f"validation:{self.notebook.plan_version}"
        self.artifacts[reference] = {"result": result, "issues": issues, "route_plan": route_plan, "mandatory": mandatory, "validation": validation}
        self.notebook.latest_validation_reference = reference
        self.last_validation = self.artifacts[reference]
        self.validation_count += 1
        return self._observe("validate_trip", "ok", "Deterministic trip validation completed.", {"validation_ref": reference, "status": validation["status"], "issue_codes": [item.code for item in issues]})

    def finalize_current_candidate_budget(self) -> None:
        """Attach the one allowed budget pass without rebuilding unchanged days."""
        if self.last_validation is None:
            raise RuntimeError("Cannot finalize budget before deterministic validation.")
        result = self.last_validation["result"]
        categories = self.calculate_budget()["categories"]
        result.budget_summary = self._budget_summary(categories)
        self.last_validation["validation"] = final_validation(
            result,
            self.world,
            self.last_validation["route_plan"],
            self.last_validation["issues"],
            self.last_validation["mandatory"],
        )
        result.status = (
            "complete" if self.last_validation["validation"]["publishable"] else "best_effort"
        )
        self.candidate_result = self.last_validation

    def build_deadline_draft(self, reason: str = "planner_deadline_reached") -> dict[str, Any]:
        """Return the safe skeleton as a visible draft without further I/O.

        This is used only after the shared planner deadline has expired.  It
        deliberately keeps exact routes and timings unknown rather than making
        one last provider call after the deadline.
        """
        if self.world is None or not self.notebook.days:
            raise RuntimeError("A safe planning skeleton is required before a deadline draft.")
        self.fallback_reason = reason
        self.completion_path = "safe_baseline_deadline_draft"
        self.termination_reason = reason
        poi_by_id = {poi.id: poi for poi in self.world.input.pois}
        stop_by_id = {stop.id: stop for stop in self.world.input.stops}
        start_date = date.fromisoformat(self.world.input.start_date)
        prior_end = "origin_anchor"
        days: list[DayPlan] = []
        chains: list[DayRouteChain] = []
        mandatory: dict[str, dict[str, Any]] = {}
        for draft in sorted(self.notebook.days, key=lambda item: item.day_number):
            end_id = draft.base_stop_id or prior_end
            if end_id == "origin_base":
                end_id = "origin_anchor"
            if draft.day_number == self.world.input.duration_days and draft.day_kind.value != "route":
                end_id = "end_anchor"
            selected = [poi_id for poi_id in draft.selected_poi_ids if poi_id in poi_by_id]
            nodes = list(dict.fromkeys([prior_end, *selected, end_id]))
            base = stop_by_id.get(draft.base_stop_id or "")
            day = DayPlan(
                day_number=draft.day_number,
                date=(start_date + timedelta(days=draft.day_number - 1)).isoformat(),
                title=draft.theme or "Trip day",
                overnight_base=base.name if base else None,
                day_start="09:00",
                day_end="18:00",
                start_anchor_id=prior_end,
                end_anchor_id=end_id,
                start_anchor=prior_end,
                end_anchor=end_id,
                day_kind=draft.day_kind,
                schedule_state="partial_timing",
                route_node_ids=nodes,
                warnings=["Exact route and timing checks stopped at the planner deadline."],
                confirm_before_visit=[
                    f"Confirm timing and travel for {poi_by_id[poi_id].name}."
                    for poi_id in selected
                ],
            )
            from ..models.planning import Activity
            day.provisional_activities = [Activity(
                poi_id=poi_id, name=poi_by_id[poi_id].name, start_time="unknown", end_time="unknown",
                planned_visit_minutes=poi_by_id[poi_id].recommended_visit_minutes or poi_by_id[poi_id].visit_minutes or 60,
                mandatory=poi_id in self.world.mandatory_poi_ids,
                requirement_ids=poi_by_id[poi_id].requirement_ids,
                opening_hours_status="requires_confirmation",
                notes=["Unscheduled proposal retained after planner timeout."],
                evidence_ids=poi_by_id[poi_id].evidence_ids,
            ) for poi_id in selected]
            days.append(day)
            chains.append(
                DayRouteChain(
                    day_number=day.day_number,
                    start_anchor_id=prior_end,
                    end_anchor_id=end_id,
                    day_kind=day.day_kind,
                    ordered_node_ids=nodes,
                    structural_complete=False,
                    missing_leg_count=max(0, len(nodes) - 1),
                    warnings=list(day.warnings),
                )
            )
            prior_end = end_id
        for poi_id in self.world.mandatory_poi_ids:
            allocated_day = next(
                (draft.day_number for draft in self.notebook.days if poi_id in draft.selected_poi_ids),
                None,
            )
            mandatory[poi_id] = (
                {
                    "status": "ALLOCATED_UNRESOLVED",
                    "day_number": allocated_day,
                    "reason": "Planner deadline reached before exact route and timing verification.",
                }
                if allocated_day is not None
                else {"status": "UNALLOCATED", "day_number": None}
            )
        deadline_issue = ValidationIssue(
            code="PLANNER_DEADLINE_REACHED",
            severity="hard",
            message="Planner deadline reached; this safe day-level draft needs route and timing confirmation.",
        )
        route_plan = TripRoutePlan(
            days=chains,
            structural_complete=False,
            timing_complete=False,
            metrics={
                "missing_legs": sum(chain.missing_leg_count for chain in chains),
                "deadline_draft": True,
            },
            warnings=[deadline_issue.message],
        )
        requirement_ids = [requirement.id for requirement in self.world.input.requirements]
        result = PlanningResult(
            status="best_effort",
            trip_summary=f"{self.world.input.trip_name} — {self.world.input.duration_days}-Day Itinerary",
            selected_bases=[
                stop_by_id[item].name if item in stop_by_id else item
                for item in self.notebook.base_sequence
            ],
            nights_by_base=dict(self.notebook.nights_by_base),
            days=days,
            unscheduled_optional_pois=list(self.world.optional_poi_ids),
            satisfied_mandatory_requirements=[],
            unsatisfied_mandatory_requirements=requirement_ids,
            assumptions=list(self.world.input.assumptions),
            uncertainties=[
                *self.world.input.unresolved_gaps,
                "Planner time limit reached before exact routing and scheduling could finish.",
            ],
            warnings=[deadline_issue.message],
            budget_summary=BudgetSummary(
                currency=self.world.input.currency,
                status="unknown",
                categories={"all_categories": {"status": "unknown", "amount": None}},
                unknown_categories=["all_categories"],
            ),
            planning_metrics={
                "planner_agent": True,
                "deadline_exhausted": True,
                "completion_path": self.completion_path,
            },
        )
        validation = final_validation(result, self.world, route_plan, [deadline_issue], mandatory)
        candidate = {
            "result": result,
            "issues": [deadline_issue],
            "route_plan": route_plan,
            "mandatory": mandatory,
            "validation": validation,
        }
        self.last_validation = self.candidate_result = candidate
        return candidate

    async def submit_itinerary(self, submission: PlanningSubmission) -> dict[str, Any]:
        """Finish only from the latest deterministic validation, never model-written timings."""
        if self.last_validation is None:
            return self._observe("submit_itinerary", "invalid", "Call validate_trip before submission.")
        self.notebook.unresolved_items = list(dict.fromkeys([*self.notebook.unresolved_items, *submission.unresolved_items]))
        self.notebook.guidance_items = list(dict.fromkeys([*self.notebook.guidance_items, *submission.guidance_items]))
        self.candidate_result = self.last_validation
        self.submitted = True
        self.completion_path = "agent_submit"
        self.termination_reason = "agent_submission"
        self.submission_count += 1
        return self._observe("submit_itinerary", "submitted", "Validated itinerary submitted.", {"validation_status": self.last_validation["validation"]["status"]})

    async def fallback_submit(self, reason: str = "agent_limit") -> None:
        """Compile a valid checkpoint or a minimal grounded scaffold without erasing work."""
        self.fallback_reason = reason
        if self.notebook.days and not self._validate_checkpoint(
            PlanningCheckpoint(
                base_sequence=self.notebook.base_sequence,
                nights_by_base=self.notebook.nights_by_base,
                stay_area_by_night=self.notebook.stay_area_by_night,
                days=self.notebook.days,
                alternatives=self.notebook.alternatives,
                unresolved_items=self.notebook.unresolved_items,
                guidance_items=self.notebook.guidance_items,
            )
        ):
            self.completion_path = "checkpoint_at_limit"
        else:
            strategy = normalize_allocation(deterministic_strategy(self.world), self.world)
            # The deterministic strategy is intentionally used only when no
            # semantically valid agent checkpoint exists.
            self.notebook = PlanningNotebook(plan_version=max(1, self.notebook.plan_version + 1), base_sequence=strategy.base_sequence, nights_by_base=strategy.nights_by_base, days=[
                SemanticDayDraft(day_number=day.day_number, base_stop_id=day.base_stop_id, day_kind=day.kind, theme=day.theme, selected_poi_ids=day.candidate_poi_ids)
                for day in strategy.days
            ])
            self.completion_path = "safe_scaffold"
            self.notebook.unresolved_items.append("Planner agent did not provide a valid semantic checkpoint; a minimal grounded scaffold was used.")
        await self.validate_trip()
        self.candidate_result = self.last_validation
        self.submitted = True
        self.termination_reason = reason

    def tools(self) -> list[StructuredTool]:
        def build(name, description, func=None, coroutine=None):
            return StructuredTool.from_function(name=name, description=description, func=func, coroutine=coroutine, handle_validation_error=True)
        return [
            build("checkpoint_plan", "Checkpoint semantic base, day and supplied POI decisions.", func=self.checkpoint_plan),
            build("calculate_route", "Calculate one factual route between supplied IDs.", coroutine=self.calculate_route),
            build("optimize_day_route", "Deterministically order a supplied day POI list without selecting POIs.", coroutine=self.optimize_day_route),
            build("simulate_day", "Calculate one exact deterministic day timeline from checkpointed decisions.", coroutine=self.simulate_day),
            build("calculate_budget", "Calculate evidence-backed budget arithmetic; unknown stays unknown.", func=self.calculate_budget),
            build("validate_trip", "Run deterministic schedule, route, budget and hard-constraint validation.", coroutine=self.validate_trip),
            build("submit_itinerary", "Submit only after deterministic validation.", coroutine=self.submit_itinerary),
        ]

    def openai_schemas(self) -> list[dict[str, Any]]:
        return [{"type": "function", "name": tool.name, "description": tool.description, "parameters": tool.args_schema.model_json_schema()} for tool in self.tools()]

    def metrics(self) -> dict[str, Any]:
        return {
            "planner_tool_calls_by_name": dict(self.tool_calls),
            "planner_checkpoint_count": self.checkpoint_count,
            "planner_submission_count": self.submission_count,
            "day_simulation_count": self.day_simulation_count,
            "route_optimizer_count": self.route_optimizer_count,
            "budget_calculation_count": self.budget_calculation_count,
            "validation_count": self.validation_count,
            "full_materialization_count": self.materialization_count,
            "route_batch_count": self.route_batch_count,
            "completion_path": self.completion_path,
            "fallback_reason": self.fallback_reason,
            "last_valid_plan_version": self.last_valid_plan_version,
            "termination_reason": self.termination_reason,
        }
