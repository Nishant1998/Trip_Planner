"""Free, standalone data tool: optimize_route(dict) -> JSON-compatible dict.

Agent integration: await asyncio.to_thread(optimize_route, payload). This file
does not register itself or change the existing six-agent workflow.
Required input: start_location, required_locations, start_date, end_date.
Locations have id/name/latitude/longitude/timezone; end_location defaults to start.
Optional: budget, transport_budget, adults/children, allowed_modes, connections,
car_cost, fixed_order, fetch_road_data. Connections are sourced agent data;
train/bus/flight services are NEVER inferred from geographic distance.
Dates select eligible fare snapshots only; they never limit trip duration.
Explicit terminal_transfer_hubs permit auditable return transfers. Optional
normalized_weighted Balanced scoring and soft/hard budgets preserve caller intent.

Example: python route_optimizer.py --sample --output SAMPLE.json
--online explicitly opts into the free OSRM road matrix. The sample alone can
resolve its nine named public landmarks using Nominatim (not a generic search
API): https://operations.osmfoundation.org/policies/nominatim/ . Public services
require attribution, caching, one request/second per application, and modest
use. Requests are serialized across processes sharing this cache. No autocomplete,
bulk POI search, paid API, environment credentials, or LLM fallback is used.
OSRM policy: https://routing.openstreetmap.de/about.html . Agent callers should
normally supply resolved locations and cached connections, with network disabled.
Prices, city visit times, opening hours and provisional dates are not bookings.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from datetime import UTC, date, datetime
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

# A sibling http.py otherwise shadows the standard library during direct CLI use.
if __package__ in (None, ""):
    sys.path = [entry for entry in sys.path if Path(entry or ".").resolve() != Path(__file__).resolve().parent]

import httpx
from ortools.sat.python import cp_model
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

CACHE = Path(__file__).resolve().parents[1] / "dev_runs" / "route_optimizer_cache.sqlite3"
OSRM = "https://routing.openstreetmap.de/routed-car/table/v1/driving/"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
ATTRIBUTION = "OpenStreetMap contributors (ODbL); road routing by OSRM/FOSSGIS"


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Location(Record):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    country_code: str = "IN"
    timezone: str = "Asia/Kolkata"
    provider_reference: str | None = None
    kind: Literal["city", "poi"] = "city"
    visit_minutes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_timezone(self):
        ZoneInfo(self.timezone)
        return self


class CarCost(Record):
    per_km_low: float = Field(ge=0)
    per_km_high: float = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    vehicles: int = Field(default=1, ge=1)
    basis: str = Field(min_length=1)
    excluded_components: list[str] = Field(default_factory=lambda: ["tolls", "parking"])

    @model_validator(mode="after")
    def range_order(self):
        if self.per_km_high < self.per_km_low:
            raise ValueError("per_km_high must be at least per_km_low")
        return self


class Connection(Record):
    id: str = Field(min_length=1)
    origin_id: str
    destination_id: str
    mode: Literal["car", "train", "bus", "flight"]
    duration_s: int = Field(gt=0)
    access_wait_s: int = Field(default=0, ge=0)
    distance_m: int | None = Field(default=None, ge=0)
    fare_amount: float | None = Field(default=None, ge=0)
    fare_high: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    fare_classification: Literal["quoted", "estimated", "unknown"] = "unknown"
    fare_scope: Literal["passenger_group", "per_adult", "per_vehicle", "unknown"] = "unknown"
    departure: datetime | None = None
    departure_date: date | None = None
    source_url: str = Field(min_length=1)
    retrieved_at: datetime | None = None
    basis: str = "Source-provided connection; access/wait allowance supplied by caller."
    unknown_components: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def coherent_price(self):
        if self.fare_high is not None and (self.fare_amount is None or self.fare_high < self.fare_amount):
            raise ValueError("fare_high requires an ordered fare_amount")
        if self.fare_classification == "unknown" and self.fare_amount is not None:
            raise ValueError("a fare amount needs quoted/estimated classification")
        if self.fare_classification != "unknown" and (self.fare_amount is None or not self.currency):
            raise ValueError("priced connections require amount and currency")
        if self.fare_classification == "quoted" and not (self.departure or self.departure_date):
            raise ValueError("quoted fares must identify their travel date")
        if self.departure and self.departure.utcoffset() is None:
            raise ValueError("departure must include a timezone offset")
        if self.retrieved_at and self.retrieved_at.utcoffset() is None:
            raise ValueError("retrieved_at must include a timezone offset")
        return self


class RouteInput(Record):
    start_location: Location
    required_locations: list[Location] = Field(default_factory=list)
    end_location: Location | None = None
    start_date: date
    end_date: date
    adults: int = Field(default=1, ge=1)
    children: int = Field(default=0, ge=0)
    budget: float | None = Field(default=None, ge=0)
    transport_budget: float | None = Field(default=None, ge=0)
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    allowed_modes: list[Literal["car", "train", "bus", "flight"]] = Field(
        default_factory=lambda: ["car", "train", "bus", "flight"]
    )
    connections: list[Connection] = Field(default_factory=list)
    car_cost: CarCost | None = None
    fetch_road_data: bool = False
    fixed_order: bool = False
    max_car_leg_hours: float = Field(default=6, gt=0, le=24)
    route_preference: Literal["cheapest", "balanced", "fastest"] = "balanced"
    reference_travel_minutes: int | None = Field(default=None, gt=0)
    reference_source: str | None = None
    date_semantics: Literal["pricing_only"] = "pricing_only"
    terminal_transfer_hubs: list[Location] = Field(default_factory=list)
    vehicle_count: int | None = Field(default=None, ge=1)
    car_capacity: int | None = Field(default=None, ge=1)
    budget_enforcement: Literal["soft", "hard"] = "soft"
    budget_scope: Literal["whole_trip", "transport"] = "whole_trip"
    balanced_policy: Literal["budget_time", "normalized_weighted"] = "budget_time"
    balanced_cost_norm: int = Field(default=120000, ge=1)
    balanced_time_norm: int = Field(default=3000, ge=1)

    @model_validator(mode="after")
    def coherent_input(self):
        if self.end_date < self.start_date or not self.allowed_modes:
            raise ValueError("dates must be ordered and allowed_modes nonempty")
        points = [self.start_location, *self.required_locations, self.end_location or self.start_location, *self.terminal_transfer_hubs]
        seen = {}
        for point in points:
            if point.id in seen and point.model_dump(exclude={"visit_minutes", "kind"}) != seen[point.id]:
                raise ValueError("one location ID cannot identify different coordinates/names")
            seen[point.id] = point.model_dump(exclude={"visit_minutes", "kind"})
        if self.car_cost and self.car_cost.currency != self.currency:
            raise ValueError("car_cost must use the trip currency")
        if len({c.id for c in self.connections}) != len(self.connections):
            raise ValueError("connection IDs must be unique")
        if any(c.origin_id not in seen or c.destination_id not in seen for c in self.connections):
            raise ValueError("connection endpoints must refer to input locations")
        return self


def _fetch(url: str, params: dict, ttl_days: int):
    """Cache and serialize approved free-service requests, including rate limits."""
    key = json.dumps([url, params], sort_keys=True)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(CACHE, timeout=60) as db:
        db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, data TEXT, stamp REAL)")
        db.execute("CREATE TABLE IF NOT EXISTS rate (host TEXT PRIMARY KEY, stamp REAL)")
        db.execute("BEGIN IMMEDIATE")
        cached = db.execute("SELECT data, stamp FROM cache WHERE key=?", (key,)).fetchone()
        if cached and time.time() - cached[1] < ttl_days * 86400:
            return json.loads(cached[0]), datetime.fromtimestamp(cached[1], UTC)
        host = httpx.URL(url).host
        last = db.execute("SELECT stamp FROM rate WHERE host=?", (host,)).fetchone()
        if last:
            time.sleep(max(0, 1.1 - (time.time() - last[0])))
        db.execute("INSERT OR REPLACE INTO rate VALUES (?,?)", (host, time.time()))
        try:
            with httpx.Client(timeout=20, headers={"User-Agent": "TripPlannerRouteOptimizer/1.0 (Pune eight-temple portfolio demo)"}) as client:
                response = client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
        except Exception:
            db.commit()  # Keep rate-limit history even after network failure.
            raise
        stamp = time.time()
        db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, json.dumps(data), stamp))
        return data, datetime.fromtimestamp(stamp, UTC)


def _road_connections(points: list[Location], request: RouteInput):
    unique = list({p.id: p for p in points}.values())
    coords = ";".join(f"{p.longitude},{p.latitude}" for p in unique)
    data, stamp = _fetch(OSRM + coords, {"annotations": "duration,distance"}, 7)
    if data.get("code") != "Ok":
        raise ValueError("OSRM could not produce a road matrix")
    count = len(unique)
    if any(len(data.get(key, [])) != count or any(len(row) != count for row in data[key])
           for key in ("durations", "distances")):
        raise ValueError("OSRM returned an invalid road matrix")
    results = []
    for i, origin in enumerate(unique):
        for j, destination in enumerate(unique):
            duration, distance = data["durations"][i][j], data["distances"][i][j]
            if i == j or duration is None or distance is None:
                continue
            if not (math.isfinite(duration) and math.isfinite(distance) and duration > 0 and distance >= 0):
                raise ValueError("OSRM returned an invalid distance/duration")
            cost = request.car_cost
            results.append(Connection(
                id=f"osrm:{origin.id}:{destination.id}", origin_id=origin.id, destination_id=destination.id,
                mode="car", duration_s=math.ceil(duration), distance_m=math.ceil(distance),
                fare_amount=distance / 1000 * cost.per_km_low if cost else None,
                fare_high=distance / 1000 * cost.per_km_high if cost else None,
                currency=request.currency if cost else None,
                fare_classification="estimated" if cost else "unknown", fare_scope="passenger_group",
                source_url="https://routing.openstreetmap.de/", retrieved_at=stamp,
                basis=cost.basis if cost else "OSRM road time/distance only; no car price supplied.",
                unknown_components=cost.excluded_components if cost else ["car operating cost"],
            ))
            if cost:
                results[-1].fare_amount *= cost.vehicles
                results[-1].fare_high *= cost.vehicles
    return results


def _amounts(connection: Connection, request: RouteInput):
    if connection.fare_amount is None or connection.currency != request.currency:
        return None, None
    if connection.fare_scope == "per_adult":
        multiplier = request.adults
    elif connection.fare_scope == "per_vehicle":
        multiplier = request.vehicle_count or (request.car_cost.vehicles if request.car_cost else 1)
    elif connection.fare_scope == "passenger_group":
        multiplier = 1
    else:
        multiplier = None
    if multiplier is None:
        return None, None
    return (round(connection.fare_amount * multiplier, 2),
            round((connection.fare_high if connection.fare_high is not None else connection.fare_amount) * multiplier, 2))


def _cents(amount: float):
    return int((Decimal(str(amount)) * 100).to_integral_value(rounding=ROUND_CEILING))


def _offers(request, points, connections):
    """Directed originals plus explicitly allowed terminal-hub suffixes only."""
    pairs, excluded, unknown_prices = {}, [], []
    vehicles = request.vehicle_count or (request.car_cost.vehicles if request.car_cost else 1)
    for offer in connections:
        reason = None
        priced_date = offer.departure_date or (offer.departure.date() if offer.departure else None)
        if offer.mode not in request.allowed_modes:
            reason = "mode_not_allowed"
        elif offer.mode == "car" and offer.duration_s + offer.access_wait_s > request.max_car_leg_hours * 3600:
            reason = "car_leg_duration_limit"
        elif offer.mode == "car" and request.car_capacity and request.adults + request.children > vehicles * request.car_capacity:
            reason = "insufficient_vehicle_capacity"
        elif priced_date and not request.start_date <= priced_date <= request.end_date:
            reason = "fare_date_outside_price_window"
        if reason:
            excluded.append({"connection_id": offer.id, "reason": reason})
            continue
        low, high = _amounts(offer, request)
        missing = list(offer.unknown_components)
        if high is None:
            missing.append("fare_currency_or_unit_unknown")
        if offer.fare_scope == "per_adult" and request.children:
            missing.append("child_fare_unknown")
        entry = {"offer": offer, "low": low, "high": high, "missing": missing}
        pairs.setdefault((offer.origin_id, offer.destination_id), []).append(entry)
        if missing:
            unknown_prices.append({"connection_id": offer.id, "mode": offer.mode, "currency": offer.currency,
                                   "known_subtotal_inr": high, "unknown_components": missing})
    candidates = []
    for i, origin in enumerate(points[:-1]):
        for j, destination in enumerate(points[1:], 1):
            if i == j or request.fixed_order and j != i + 1:
                continue
            paths = [[entry] for entry in pairs.get((origin.id, destination.id), [])]
            if j == len(points) - 1:
                for hub in request.terminal_transfer_hubs:
                    if hub.id not in {origin.id, destination.id}:
                        paths.extend([a, b] for a in pairs.get((origin.id, hub.id), [])
                                     for b in pairs.get((hub.id, destination.id), []))
            for entries in paths:
                candidates.append({"i": i, "j": j, "entries": entries,
                    "cost": sum(_cents(e["high"]) for e in entries if e["high"] is not None),
                    "unknown": sum(bool(e["missing"]) for e in entries),
                    "minutes": sum(math.ceil((e["offer"].duration_s + e["offer"].access_wait_s) / 60) for e in entries)})
    return candidates, excluded, unknown_prices


def _search(request, points, candidates, profile, cap, complete_only):
    """Additive CP-SAT path. Explicit degrees retain every mandatory vertex."""
    eligible = [c for c in candidates if not complete_only or not c["unknown"]]
    if not eligible:
        return None
    model = cp_model.CpModel()
    choices = [(c, model.new_bool_var(f"leg_{k}")) for k, c in enumerate(eligible)]
    model.add_circuit([(len(points) - 1, 0, model.new_constant(1))] +
                      [(c["i"], c["j"], v) for c, v in choices])
    for i in range(len(points) - 1):
        model.add(sum(v for c, v in choices if c["i"] == i) == 1)
    for j in range(1, len(points)):
        model.add(sum(v for c, v in choices if c["j"] == j) == 1)
    cost = sum(c["cost"] * v for c, v in choices)
    minutes = sum(c["minutes"] * v for c, v in choices)
    if cap is not None:
        model.add(cost <= _cents(cap))
    if profile in {"cheapest", "balanced_over_budget"}:
        objectives = [cost, minutes]
    elif profile == "balanced" and request.balanced_policy == "normalized_weighted":
        divisor = math.gcd(request.balanced_time_norm, request.balanced_cost_norm * 100)
        objectives = [cost * (request.balanced_time_norm // divisor) + minutes * (request.balanced_cost_norm * 100 // divisor), cost, minutes]
    else:
        objectives = [minutes, cost]
    if not complete_only:
        objectives.insert(0, sum(c["unknown"] * v for c, v in choices))
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    solver.parameters.max_deterministic_time = 2
    solver.parameters.max_time_in_seconds = 10
    proven = True
    for objective in objectives:
        model.minimize(objective)
        status = solver.solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return None
        if status != cp_model.OPTIMAL:
            proven = False
            break
        model.add(objective == solver.value(objective))
    return {c["i"]: c for c, v in choices if solver.value(v)}, proven


def _failure(points, candidates, profile):
    adjacency = {i: set() for i in range(len(points))}
    reverse = {i: set() for i in range(len(points))}
    for c in candidates:
        adjacency[c["i"]].add(c["j"])
        reverse[c["j"]].add(c["i"])
    def reached(graph, initial):
        seen, todo = {initial}, [initial]
        while todo:
            for node in graph[todo.pop()] - seen:
                seen.add(node)
                todo.append(node)
        return seen
    forward, backward = reached(adjacency, 0), reached(reverse, len(points) - 1)
    disconnected = [p.id for i, p in enumerate(points[1:-1], 1) if i not in forward or i not in backward]
    return {"profile": profile, "status": "DISCONNECTED_REQUIRED_STOP" if disconnected else "NO_FEASIBLE_ROUTE",
            "disconnected_location_ids": disconnected, "requested_location_ids": [p.id for p in points[1:-1]],
            "reason": "No complete path using eligible directed offers; no requested stop removed."}


def _solve(request, points, connections, profile, cap=None):
    candidates, excluded, unknown_prices = _offers(request, points, connections)
    trivial = len(points) == 2 and points[0].id == points[-1].id
    search = ({}, True) if trivial else _search(request, points, candidates, profile, cap, True)
    if search is None and request.budget_enforcement != "hard":
        search = _search(request, points, candidates, profile, cap, False)
    if search is None:
        return {**_failure(points, candidates, profile), "excluded_connections": excluded}
    chosen, proven = search
    legs, index = [], 0
    names = {p.id: p.name for p in [*points, *request.terminal_transfer_hubs]}
    while not trivial and index != len(points) - 1:
        candidate = chosen[index]
        for entry in candidate["entries"]:
            offer = entry["offer"]
            legs.append({"origin_id": offer.origin_id, "destination_id": offer.destination_id,
                "origin": names.get(offer.origin_id, offer.origin_id), "destination": names.get(offer.destination_id, offer.destination_id),
                "connection_id": offer.id, "mode": offer.mode, "distance_m": offer.distance_m,
                "duration_s": math.ceil((offer.duration_s + offer.access_wait_s) / 60) * 60,
                "departure": None, "arrival": None, "timing_status": "unscheduled",
                "duration_basis": "OSRM baseline; traffic unverified" if offer.id.startswith("osrm:") else "Supplied elapsed travel time including declared access/wait",
                "cost_low": entry["low"], "cost_high": entry["high"], "currency": request.currency,
                "source_fare_amount": offer.fare_amount, "source_fare_currency": offer.currency,
                "source_fare_scope": offer.fare_scope, "cost_scope": "passenger_group",
                "fare_classification": offer.fare_classification if not entry["missing"] else "unknown",
                "price_status": "known_synthetic" if offer.source_url.startswith("fixture:") and not entry["missing"] else "unknown" if entry["missing"] else offer.fare_classification,
                "unknown_components": entry["missing"], "basis": offer.basis,
                "evidence": {"source_url": offer.source_url, "retrieved_at": offer.retrieved_at.isoformat() if offer.retrieved_at else None}})
        index = candidate["j"]
    path = [points[0].id, *[leg["destination_id"] for leg in legs]]
    required = {p.id for p in points[1:-1]}
    missing = [f'{leg["connection_id"]}: {item}' for leg in legs for item in leg["unknown_components"]]
    low = round(sum(leg["cost_low"] for leg in legs if leg["cost_low"] is not None), 2)
    high = round(sum(leg["cost_high"] for leg in legs if leg["cost_high"] is not None), 2)
    travel = sum(leg["duration_s"] for leg in legs) // 60
    limit = request.transport_budget if request.budget_scope == "transport" and request.transport_budget is not None else request.budget
    exceeded = limit is not None and high > limit
    return {"profile": profile, "status": "complete" if trivial else "partial" if missing or any(leg["fare_classification"] != "quoted" for leg in legs) else "complete",
        "solver_status": "OPTIMAL" if proven else "FEASIBLE", "optimality_proven": proven, "optimality_method": "additive_CP_SAT",
        "optimality_scope": "supplied directed offers and priced components; no live-booking or timetable claim",
        "global_cheapest_confirmed": profile == "cheapest" and proven and not unknown_prices and not missing,
        "city_order": path, "ordered_required_visits": [p for p in path if p in required],
        "transfer_hubs": list(dict.fromkeys(p for p in path[1:-1] if p not in required and p != points[-1].id)),
        "legs": legs, "travel_minutes": travel, "visit_minutes": 0, "elapsed_minutes": None,
        "time_confidence": "baseline_estimate" if any(leg["connection_id"].startswith("osrm:") for leg in legs) else "source_estimate",
        "vehicle_count": request.vehicle_count or (request.car_cost.vehicles if request.car_cost else 1),
        "pricing_coverage": {"complete_for_selected": not missing, "unknown_alternatives": unknown_prices},
        "excluded_connections": excluded,
        "profile_score": .5 * high / request.balanced_cost_norm + .5 * travel / request.balanced_time_norm if profile == "balanced" and request.balanced_policy == "normalized_weighted" else None,
        "cost": {"currency": request.currency, "known_quoted_subtotal": round(sum(leg["cost_high"] for leg in legs if leg["fare_classification"] == "quoted"), 2),
            "subtotal_low": low, "subtotal_high": high, "total_low": None if missing else low, "total_high": None if missing else high,
            "unknown_components": missing, "budget_status": "over" if exceeded else "unknown" if missing or limit is None else "within",
            "budget_scope": request.budget_scope, "budget_exceeded": exceeded, "trip_budget_status": "unknown", "other_trip_costs": "not_modelled",
            "excess_over_total_budget": round(max(0, high - request.budget), 2) if request.budget is not None else None,
            "preferred_limit": request.transport_budget if request.transport_budget is not None else round(request.budget * .35, 2) if request.budget is not None else None}}


def optimize_route(payload: dict) -> dict:
    """Data-only outline. Price windows never impose a travel deadline."""
    try:
        request = RouteInput.model_validate(payload)
    except (ValidationError, ValueError, KeyError) as exc:
        return {"schema_version": "transport_routes.v1", "status": "error", "error_code": "invalid_input", "warnings": [str(exc)]}
    end = request.end_location or request.start_location
    stops = list({p.id: p for p in request.required_locations if p.id not in {request.start_location.id, end.id}}.values())
    points, connections = [request.start_location, *stops, end], list(request.connections)
    warnings = ["Route outline only; no sightseeing or real timetable chronology is modelled.",
                "Dates select fares only. Other trip expenses are not modelled."]
    if request.fetch_road_data and "car" in request.allowed_modes:
        try:
            connections += _road_connections(points, request)
        except (httpx.HTTPError, ValueError, sqlite3.Error) as exc:
            warnings.append(f"Free road data unavailable: {type(exc).__name__}; supplied offers retained.")
    if len({c.id for c in connections}) != len(connections):
        return {"schema_version": "transport_routes.v1", "status": "error", "error_code": "duplicate_connection_id", "warnings": warnings}
    cheapest = _solve(request.model_copy(update={"budget_enforcement": "soft"}), points, connections, "cheapest")
    if request.budget_enforcement == "hard" and request.budget is not None and "cost" in cheapest and cheapest["cost"]["total_high"] is not None and cheapest["cost"]["total_high"] > request.budget and not cheapest["pricing_coverage"]["unknown_alternatives"]:
        profiles = [{"profile": name, "status": "INFEASIBLE_BUDGET", "requested_location_ids": [p.id for p in stops],
                     "cost_lower_bound_inr": cheapest["cost"]["total_high"],
                     "diagnostic_candidate": {**cheapest, "violates_hard_budget": True}}
                    for name in ("cheapest", "balanced", "fastest")]
    else:
        hard_cap = request.budget if request.budget_enforcement == "hard" else None
        if hard_cap is not None:
            cheapest = _solve(request, points, connections, "cheapest", hard_cap)
        if request.balanced_policy == "normalized_weighted":
            balanced = _solve(request, points, connections, "balanced", hard_cap)
        else:
            preferred = request.transport_budget if request.transport_budget is not None else request.budget * .35 if request.budget is not None else None
            balanced = _solve(request, points, connections, "balanced", hard_cap if hard_cap is not None else preferred)
            if "legs" not in balanced and preferred is not None and hard_cap is None:
                balanced = _solve(request, points, connections, "balanced", round(preferred * 1.1, 2))
            if "legs" not in balanced and hard_cap is None:
                balanced = _solve(request, points, connections, "balanced_over_budget")
                balanced["profile"] = "balanced"
                balanced["selection_note"] = "No route inside the priced-component allowance; mandatory stops preserved."
        profiles = [cheapest, balanced, _solve(request, points, connections, "fastest", hard_cap)]
    valid = [p for p in profiles if "legs" in p]
    recommended = next((p for p in valid if p["profile"] == request.route_preference), valid[0] if valid else None)
    groups = {}
    for profile in valid:
        signature = tuple((leg["connection_id"], leg["mode"]) for leg in profile["legs"])
        groups.setdefault(signature, []).append(profile["profile"])
    comparison = {"reference_minutes": request.reference_travel_minutes, "source": request.reference_source,
                  "status": "unresolved_source_or_scope",
                  "model_travel_minutes": {p["profile"]: p["travel_minutes"] for p in valid}} if request.reference_travel_minutes else None
    return {"schema_version": "transport_routes.v1", "status": profiles[0]["status"] if not valid else "partial" if any(p["status"] != "complete" for p in profiles) else "complete",
        "locations": [p.model_dump(mode="json") for p in dict((p.id, p) for p in [*points, *request.terminal_transfer_hubs]).values()],
        "profiles": profiles, "warnings": warnings, "attribution": ATTRIBUTION, "route_audit": None,
        "price_lookup_window": {"from": request.start_date.isoformat(), "to": request.end_date.isoformat(), "purpose": "fare_lookup_only"},
        "reference_comparison": comparison, "equivalent_profiles": [names for names in groups.values() if len(names) > 1],
        "recommendation": {"profile": recommended["profile"] if recommended else None, "status": "provisional" if recommended else "unavailable",
            "auto_select": False, "use_for": "provisional_planning_outline", "reason": "Declared objective; all alternatives and unknown costs retained."},
        "budget_policy": {"target": request.budget * .3 if request.budget is not None else None,
            "preferred_limit": request.transport_budget if request.transport_budget is not None else request.budget * .35 if request.budget is not None else None,
            "soft_limit": round(request.budget * .385, 2) if request.budget is not None else None,
            "warning_limit": request.budget * .4 if request.budget is not None else None},
        "paid_api_calls": 0, "llm_calls": 0}


def from_trip_memory(memory: dict, resolved_locations: dict[str, dict], **options) -> dict:
    """Adapt the planner's TripRequest fields without changing confirmed memory.

    resolved_locations keys must match origin/destination labels from memory.
    The agent supplies any end_location and provider connection records explicitly.
    """
    return {"start_location": resolved_locations[memory["origin"]],
            "required_locations": [resolved_locations[name] for name in memory["destinations"]],
            "start_date": memory["start_date"], "end_date": memory["end_date"],
            "adults": memory.get("adults", 1), "children": memory.get("children", 0),
            "budget": memory.get("budget"), "currency": memory.get("currency") or "INR", **options}


def _check_result(payload: dict, result: dict):
    """Focused recurrence guards, explicitly requested by --check/--sample."""
    request = RouteInput.model_validate(payload)
    end = request.end_location or request.start_location
    required = {p.id for p in request.required_locations} - {request.start_location.id, end.id}
    assert {p["profile"] for p in result["profiles"]} == {"cheapest", "balanced", "fastest"}
    assert result["recommendation"]["profile"] in {p["profile"] for p in result["profiles"] if "legs" in p}
    for profile in result["profiles"]:
        if "legs" not in profile:
            continue
        legs, order = profile["legs"], profile["city_order"]
        assert order[0] == request.start_location.id and order[-1] == end.id
        assert set(order[1:-1]) == required and len(order[1:-1]) == len(required)
        assert all(a["destination_id"] == b["origin_id"] for a, b in zip(legs, legs[1:]))
        assert profile["travel_minutes"] == sum(leg["duration_s"] for leg in legs) // 60
        assert profile["cost"]["subtotal_high"] == round(sum(leg["cost_high"] for leg in legs if leg["cost_high"] is not None), 2)
        if profile["cost"]["unknown_components"]:
            assert profile["cost"]["budget_status"] == "unknown" and profile["cost"]["total_high"] is None
        if any(leg["connection_id"].startswith("osrm:") for leg in legs):
            assert profile["time_confidence"] == "baseline_estimate"
        if result["route_audit"]:
            best = result["route_audit"]["best_orders"]
            expected_order = (best.get("balanced") or best.get("balanced_soft") or best.get("cheapest")) if profile["profile"] == "balanced" else best.get(profile["profile"])
            assert order == expected_order, "Profile does not match the independently audited objective"
    assert result["paid_api_calls"] == result["llm_calls"] == 0
    return {"all_required_stops": True, "continuous_route": True, "consistent_totals": True,
            "unknown_costs_preserved": True, "time_basis_labelled": True, "recommendation_present": True,
            "all_three_profiles_retained": True, "profile_objectives_match_audit": bool(result["route_audit"])}


SAMPLE_QUERIES = [
    ("Pune", "Pune, Maharashtra, India"),
    ("Mayureshwar, Morgaon", "Mayureshwar Temple, Morgaon"),
    ("Siddhivinayak, Siddhatek", "Siddhivinayak Temple, Siddhatek"),
    ("Ballaleshwar, Pali", "Ballaleshwar Temple, Pali"),
    ("Varadvinayak, Mahad", "Varadvinayak Temple, Mahad"),
    ("Chintamani, Theur", "चिंतामणी मंदिर"),
    ("Girijatmaj, Lenyadri", "गिरिजात्मज"),
    ("Vighneshwar, Ozar", "विघ्नहर"),
    ("Mahaganapati, Ranjangaon", "महागणपती"),
]


def _sample(online: bool):
    """One named-landmark lookup batch; never discover or enumerate extra POIs."""
    points = []
    if not online:
        raise ValueError("First sample needs --online to resolve landmarks; later runs use the recorded --input JSON offline.")
    for index, (name, query) in enumerate(SAMPLE_QUERIES):
        print(f"Resolving {index + 1}/9: {name}", file=sys.stderr)
        viewbox = {6: "73.86,19.27,73.91,19.22", 7: "73.94,19.23,74.00,19.16",
                   8: "74.20,18.78,74.27,18.73"}.get(index, "72.8,19.5,75.5,17.7")
        results, _ = _fetch(NOMINATIM, {"q": query, "format": "jsonv2", "countrycodes": "in", "limit": 3,
                                       "viewbox": viewbox, "bounded": 1}, 90)
        if not results:
            raise ValueError(f"No resolved map landmark for {name}; no placeholder substituted.")
        candidate = results[0]
        if index and candidate.get("type") not in {"place_of_worship", "temple", "archaeological_site"}:
            raise ValueError(f"{name} resolved to an area instead of a temple; manual map confirmation needed.")
        points.append(Location(id=f"osm:{candidate['osm_type']}:{candidate['osm_id']}", name=name,
                               latitude=float(candidate["lat"]), longitude=float(candidate["lon"]),
                               provider_reference=f"https://www.openstreetmap.org/{candidate['osm_type']}/{candidate['osm_id']}",
                               kind="city" if index == 0 else "poi", visit_minutes=0 if index == 0 else 60))
    request = RouteInput(start_location=points[0], required_locations=points[1:], start_date=date(2026, 12, 4),
                      end_date=date(2026, 12, 6), budget=30000, allowed_modes=["car"], fetch_road_data=True,
                      car_cost=CarCost(per_km_low=6, per_km_high=9, currency="INR",
                                       basis="Demo fuel allowance INR 6-9/km per vehicle, caller assumption; not a taxi quote."))
    print("Fetching cached/free road matrix for the nine resolved locations", file=sys.stderr)
    request.connections = _road_connections([*points, points[0]], request)
    request.fetch_road_data = False  # Preserve the sample's actual input for offline replay.
    return request.model_dump(mode="json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", action="store_true")
    parser.add_argument("--online", action="store_true", help="Opt into the limited free sample lookups/road data.")
    parser.add_argument("--input", type=Path, help="Structured input JSON; otherwise read stdin.")
    parser.add_argument("--output", type=Path, help="Write sample evidence, refusing to overwrite an existing file.")
    parser.add_argument("--check", action="store_true", help="Run only focused route/recommendation recurrence guards.")
    args = parser.parse_args()
    try:
        payload = _sample(args.online) if args.sample else json.loads(args.input.read_text(encoding="utf-8") if args.input else sys.stdin.read())
        if "input" in payload and "output" in payload:
            payload = payload["input"]
        if not args.online:
            payload["fetch_road_data"] = False
        result = optimize_route(payload)
        if args.check or args.sample:
            result["regression_checks"] = _check_result(payload, result)
        if args.sample:
            expected = {p["id"] for p in payload["required_locations"]}
            for profile in result["profiles"]:
                assert "legs" in profile, profile
                assert set(profile["city_order"][1:-1]) == expected
                assert len(profile["city_order"][1:-1]) == 8
                assert profile["city_order"][0] == profile["city_order"][-1] == payload["start_location"]["id"]
                assert all(a["destination_id"] == b["origin_id"] for a, b in zip(profile["legs"], profile["legs"][1:]))
                assert profile["travel_minutes"] == sum(x["duration_s"] for x in profile["legs"]) // 60
                assert profile["cost"]["subtotal_high"] == round(sum(x["cost_high"] for x in profile["legs"]), 2)
                assert all(x["fare_classification"] == "estimated" for x in profile["legs"])
            result["sample_checks"] = {"all_eight_temples": True, "continuous_return_route": True,
                                       "consistent_totals": True, "estimates_labelled": True}
        if args.output:
            evidence = {"input": payload, "output": result, "recorded_at": datetime.now(UTC).isoformat()}
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(evidence, stream, indent=2, ensure_ascii=False)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1 if result["status"] == "error" else 0
    except (ValueError, KeyError, httpx.HTTPError, sqlite3.Error, OSError, AssertionError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
