"""Six utility bindings with server-owned factual inputs for numerical tools."""
from copy import deepcopy

from pydantic import BaseModel, ConfigDict

from .calculation import CalculationRequest, calculate
from .ordering import OrderRequest, optimize_stop_order
from .route_tool import RouteRequest
from .timeline import TimelineRequest, check_timeline
from .timeline import TimelineStep
from .contracts import fingerprint
from .protocol import strict_object_schema
from .currency import convert_budget


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gap_id: str


class ReadRequest(SearchRequest):
    source_id: str


class PlannerUtilities:
    def __init__(self, *, routes, web, entity_ids: set[str],
                 cost_facts: dict[str, dict], step_facts: dict[str, dict],
                 directed_minutes: dict[tuple[str, str], float | None], fx_facts=None):
        self.routes, self.web = routes, web
        self.entity_ids = frozenset(entity_ids)
        self.cost_facts = deepcopy(cost_facts)
        self.step_facts = deepcopy(step_facts)
        self.directed_minutes = dict(directed_minutes)
        self.claims = {}
        self.constraint_review = {}
        self.fx_facts = deepcopy(fx_facts or {})

    def snapshot(self):
        return {"entities": sorted(self.entity_ids), "claims": deepcopy(self.claims), "cost_facts": deepcopy(self.cost_facts),
                "constraint_review": deepcopy(self.constraint_review),
                "fx_facts": deepcopy(self.fx_facts),
                "step_facts": deepcopy(self.step_facts),
                "directed_minutes": [[list(pair), minutes] for pair, minutes in self.directed_minutes.items()],
                "routes": self.routes.snapshot() if self.routes else None,
                "web": self.web.snapshot() if self.web else None}

    def restore(self, saved):
        if saved.get("fx_facts", {}) != self.fx_facts:
            raise ValueError("Saved currency evidence differs from current research")
        if not set(saved["entities"]) <= self.entity_ids:
            raise ValueError("Utility state entity scope mismatch")
        if saved["routes"] is not None:
            if self.routes is None:
                raise ValueError("Saved routing state requires routing adapter")
            self.routes.restore(saved["routes"])
        if saved["web"] is not None:
            if self.web is None:
                raise ValueError("Saved web state requires web adapter")
            self.web.restore(saved["web"])
        self.cost_facts = deepcopy(saved["cost_facts"])
        self.claims = deepcopy(saved.get("claims", {}))
        self.constraint_review = deepcopy(saved.get("constraint_review", {}))
        self.step_facts = deepcopy(saved["step_facts"])
        self.directed_minutes = {tuple(pair): value for pair, value in saved["directed_minutes"]}

    @property
    def anchor_catalog(self):
        resolver = getattr(self.routes, "resolver", None)
        if resolver is None:
            return []
        return [{"id": node.id, "name": node.name, "base_id": node.base_id, "role": node.role,
                 "coordinates": node.coordinates.model_dump(mode="json") if node.coordinates else None}
                for node in resolver.nodes.values()]

    @property
    def handlers(self):
        return {"calculate": self.calculate, "get_routes": self.get_routes,
                "optimize_stop_order": self.optimize_stop_order, "check_timeline": self.check_timeline,
                "web_search": self.web_search, "read_webpage": self.read_webpage}

    @staticmethod
    def schemas():
        models = {"calculate": CalculationRequest, "get_routes": RouteRequest,
                  "optimize_stop_order": OrderRequest, "check_timeline": TimelineRequest,
                  "web_search": SearchRequest, "read_webpage": ReadRequest}
        descriptions = {
            "calculate": "Compute arithmetic or an itemized budget from supplied amounts; does not select or change the plan.",
            "get_routes": "Retrieve supported routes between registered endpoints at the supplied date and time; does not choose destinations.",
            "optimize_stop_order": "Order exactly the selected stops using supplied directed route facts; does not add or remove visits.",
            "check_timeline": "Simulate the proposed dated sequence and report conflicts or unknowns; does not repair the plan.",
            "web_search": "Find public source leads for a registered material gap; snippets are not verified evidence.",
            "read_webpage": "Read a registered source for the specified gap; returns untrusted excerpts, not accepted facts.",
        }
        return [{"type": "function", "name": name, "description": descriptions[name],
                 "strict": True, "parameters": strict_object_schema(model.model_json_schema())}
                for name, model in models.items()]

    def _entities(self, values):
        if not set(values) <= self.entity_ids:
            raise ValueError("Unknown entity identity")

    async def calculate(self, arguments):
        request = CalculationRequest.model_validate(arguments)
        for item in request.items:
            if item.basis in {"supplier_evidence", "estimate", "not_applicable"}:
                fact = self.cost_facts.get(item.id)
                values = item.model_dump(mode="json")
                if fact is None or any(values.get(key) != fact.get(key) for key in (
                    "lower", "upper", "quantity", "currency", "unit", "category", "basis", "evidence_ids"
                )):
                    raise ValueError("Cost does not match registered evidence")
        if request.operation == "budget":
            request, conversions = convert_budget(request, self.fx_facts)
            return {**calculate(request), "fx_conversions": conversions}
        return calculate(request)

    async def get_routes(self, arguments):
        result = await self.routes(arguments)
        registered = []
        for route in result.get("routes", []):
            identity = "travel:" + fingerprint({"route": route,
                "date": result["departure_date"], "time": result["departure_time"]})[:24]
            minutes = route.get("duration_minutes")
            service = route.get("scheduled_service")
            step = TimelineStep(id=identity, kind="travel", origin_id=route["origin_id"],
                destination_id=route["destination_id"], minutes=minutes,
                basis="unknown" if minutes is None else "evidence" if route.get("status") == "verified" else "estimate",
                fixed_start=service.get("departure_at") if service else None,
                scheduled_service=service)
            fact = step.model_dump(mode="json")
            fact["applicable_date"] = result["departure_date"]
            fact["route_mode"] = route["mode"]
            fact["route_evidence"] = deepcopy(route)
            self.step_facts[identity] = fact
            registered.append(fact)
        return {**result, "step_facts": registered}

    async def optimize_stop_order(self, arguments):
        request = OrderRequest.model_validate(arguments)
        self._entities([request.start_id, request.end_id, *request.selected_ids])
        for route in request.routes:
            pair = (route.origin_id, route.destination_id)
            self._entities(pair)
            if route.route_fact_id is not None:
                fact = self.step_facts.get(route.route_fact_id)
                if (fact is None or fact.get("kind") != "travel"
                    or (fact.get("origin_id"), fact.get("destination_id")) != pair
                    or fact.get("minutes") != route.minutes
                    or request.departure_date is None
                    or fact.get("applicable_date") != request.departure_date.isoformat()):
                    raise ValueError("Ordering cost does not match dated route evidence")
            elif route.minutes is not None and (pair not in self.directed_minutes or self.directed_minutes[pair] != route.minutes):
                raise ValueError("Ordering cost does not match registered route evidence")
        return optimize_stop_order(request)

    async def check_timeline(self, arguments):
        request = TimelineRequest.model_validate(arguments)
        self._entities([request.start_anchor_id, request.end_anchor_id])
        bound_steps = []
        for step in request.steps:
            self._entities([step.origin_id, step.destination_id])
            fact = self.step_facts.get(step.id)
            if fact is None:
                # A model may schedule rest/meals/buffers, not assert venue or
                # movement facts without a registered semantic step.
                if step.kind in {"visit", "travel"} or step.basis != "planning_allowance":
                    raise ValueError("Unregistered timeline fact")
                if step.windows or step.availability_known or step.session_confirmed:
                    raise ValueError("Unregistered availability assertion")
                bound_steps.append(step)
                continue
            values = step.model_dump(mode="json")
            if fact.get("applicable_date") and fact["applicable_date"] != request.start_time.date().isoformat():
                raise ValueError("Timeline fact belongs to another date")
            for key in ("kind", "origin_id", "destination_id"):
                if values[key] != fact.get(key):
                    raise ValueError("Timeline constraint differs from registered fact")
            # These fields are server-owned research/provider constraints.  A
            # model may omit them (the wire defaults are empty/false/None), in
            # which case bind the registered value before simulation.  It may
            # not supply a conflicting non-default value or invent evidence.
            defaults = {"minimum_minutes": None, "windows": [],
                "availability_known": False, "session_required": False,
                "session_confirmed": False, "scheduled_service": None}
            bound = {}
            for key, default in defaults.items():
                supplied, registered = values[key], fact.get(key)
                if supplied != registered and supplied != default:
                    raise ValueError("Timeline constraint differs from registered fact")
                bound[key] = registered
            if step.basis != "planning_allowance":
                if values["minutes"] != fact.get("minutes") or values["basis"] != fact.get("basis"):
                    raise ValueError("Duration differs from registered evidence")
            elif step.kind == "travel":
                raise ValueError("Missing routes cannot become automatic travel allowances")
            if fact.get("fixed_start") is not None and values["fixed_start"] != fact["fixed_start"]:
                raise ValueError("Fixed appointment changed")
            bound_steps.append(TimelineStep.model_validate({**values, **bound}))
        return check_timeline(request.model_copy(update={"steps": bound_steps}))

    async def web_search(self, arguments):
        request = SearchRequest.model_validate(arguments)
        return await self.web.search(request.gap_id)

    async def read_webpage(self, arguments):
        request = ReadRequest.model_validate(arguments)
        return await self.web.read(request.gap_id, request.source_id)
