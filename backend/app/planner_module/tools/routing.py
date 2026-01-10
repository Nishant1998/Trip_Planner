"""Lazy grounded routing with explicit transport-mode decisions."""

from __future__ import annotations

import os
import logging
from copy import copy
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from math import asin, ceil, cos, radians, sin, sqrt
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

import httpx
from app.services.provider_usage import HTTP_HOOKS

from ..models.planning import (
    Coordinates,
    ModeResolution,
    ModeSelectionStatus,
    PlannerInput,
    RouteFact,
    ScheduledService,
    TransportMode,
)
from ..tools.transport import (
    PROVIDER_MODE,
    TransportEndpoint,
    classify_movement,
    is_terminal_stop,
    normalize_mode,
    resolve_mode,
)

logger = logging.getLogger(__name__)


class RouteDispatchStopped(RuntimeError):
    """A planner budget/persistence guard rejected dispatch, not a provider failure."""


@dataclass(frozen=True)
class RouteNode:
    id: str
    name: str
    coordinates: Coordinates | None = None
    base_id: str | None = None
    region_id: str | None = None
    country_code: str | None = None
    role: str = "unknown"
    route_context: bool = False


class RouteResolver:
    """Research evidence first; never silently replace a user mode preference."""

    def __init__(
        self, data: PlannerInput, *, local_attachment_km: float = 25.0,
        request_guard=None,
    ) -> None:
        self.data = data
        self.request_guard = request_guard
        self.local_attachment_km = local_attachment_km
        self.cache: dict[tuple[str, str, str], RouteFact] = {}
        self.trace: list[dict[str, Any]] = []
        self.departure_date = data.start_date
        self.departure_time = data.time_preferences.get("preferred_day_start") or "09:00"
        self.request_results: dict[tuple, RouteFact] = {}
        base_types = {
            "base",
            "city",
            "locality",
            "region",
            "country",
            "administrative_area_level_1",
            "administrative_area_level_2",
        }
        # Research V2 marks city/base meaning explicitly through ``role``.
        # Provider primary types can legitimately remain the generic ``place``.
        base_ids = {
            stop.id
            for stop in data.stops
            if stop.role == "base" or stop.type.casefold() in base_types
        }
        has_bases = bool(base_ids)
        self.nodes = {
            stop.id: RouteNode(
                stop.id,
                stop.name,
                stop.coordinates,
                base_id=stop.id if stop.id in base_ids else None,
                role="terminal" if is_terminal_stop(stop) else "base" if stop.id in base_ids else "route_member",
                route_context=not has_bases,
            )
            for stop in data.stops
        }

        def poi_base(poi):
            if poi.stop_id in base_ids:
                return poi.stop_id
            # An independent POI stop is attached only when it is near a real
            # planning base. This is a supporting geographic signal, not a raw
            # "different IDs means intercity" rule.
            candidates = [self.nodes[x] for x in base_ids if self.nodes[x].coordinates]
            if poi.coordinates and candidates:
                nearest = min(
                    candidates,
                    key=lambda node: self._haversine_km(
                        node.coordinates, poi.coordinates
                    ),
                )
                if (
                    self._haversine_km(nearest.coordinates, poi.coordinates)
                    <= self.local_attachment_km
                ):
                    return nearest.id
            # This POI has no defensible local-base attachment.  Retain its own
            # structured stop context so a remote excursion is not mistaken for
            # a local attraction merely because it is a POI.
            return poi.stop_id

        self.nodes.update(
            {
                poi.id: RouteNode(
                    poi.id,
                    poi.name,
                    poi.coordinates,
                    base_id=poi_base(poi),
                    role="poi",
                    route_context=not has_bases,
                )
                for poi in data.pois
            }
        )
        def boundary_base(name: str) -> str | None:
            matches = [stop.id for stop in data.stops
                       if stop.id in base_ids and stop.name.strip().casefold() == name.strip().casefold()]
            return matches[0] if len(matches) == 1 else None

        self.nodes["origin_anchor"] = RouteNode(
            "origin_anchor", data.origin, base_id=boundary_base(data.origin) or "origin_anchor", role="origin"
        )
        self.nodes["end_anchor"] = RouteNode(
            "end_anchor",
            data.end_location or data.origin,
            base_id=boundary_base(data.end_location or data.origin) or "end_anchor",
            role="end",
        )
        self.terminal_pairs: dict[tuple[str, str], tuple[str, str]] = {}
        self._register_transport_endpoints()
        for route in data.routes:
            # Research-backed duration is schedule-ready when its endpoints and
            # mode are structured, even if it is an explicit estimate rather
            # than a live routing-provider response.  Keep that provenance so
            # the itinerary never presents the estimate as provider-verified.
            if route.duration_minutes is not None and route.status in {
                "verified", "estimated", "research_estimated",
            }:
                origin = (
                    route.origin_id
                    if route.origin_id in self.nodes
                    else "origin_anchor"
                )
                destination = (
                    route.destination_id
                    if route.destination_id in self.nodes
                    else "end_anchor"
                )
                self.cache[self.cache_key(origin, destination, route.mode)] = (
                    route.model_copy(
                        update={
                            "origin_id": origin,
                            "destination_id": destination,
                            "provenance": (
                                "research_verified"
                                if route.status == "verified"
                                else "research_estimated"
                            ),
                        }
                    )
                )
                pair = self.terminal_pairs.get((origin, destination))
                if pair:
                    self.cache[self.cache_key(*pair, route.mode)] = self.cache[
                        self.cache_key(origin, destination, route.mode)
                    ].model_copy(update={"origin_id": pair[0], "destination_id": pair[1]})

    def _register_transport_endpoints(self) -> None:
        """Retain evidence-linked endpoint labels without inventing station coordinates."""
        for requirement in self.data.transport_requirements:
            if requirement.get("leg_kind") not in {"entry", "exit", "intercity"}:
                continue
            if not requirement.get("route_evidence_ids"):
                continue
            left = requirement.get("origin_id")
            right = requirement.get("destination_id")
            if requirement.get("leg_kind") == "entry":
                left = "origin_anchor"
            if requirement.get("leg_kind") == "exit":
                right = "end_anchor"
            if left not in self.nodes or right not in self.nodes:
                continue
            pair = []
            for node_id, key in ((left, "origin_label"), (right, "destination_label")):
                node = self.nodes[node_id]
                label = str(requirement.get(key) or "").strip()
                matches = [stop for stop in self.data.stops
                           if is_terminal_stop(stop) and label.casefold() == stop.name.casefold()]
                if len(matches) == 1:
                    stop = matches[0]
                    self.nodes[stop.id] = RouteNode(
                        stop.id, stop.name, stop.coordinates,
                        base_id=node.base_id or node_id, role="terminal",
                    )
                    pair.append(stop.id)
                    continue
                # A different evidence-linked label is only a candidate endpoint,
                # not proof that a terminal was resolved or a service was booked.
                if not label or label.casefold() == node.name.casefold():
                    pair.append(node_id)
                    continue
                digest = sha256(f"{node_id}:{label}".encode()).hexdigest()[:12]
                terminal_id = f"terminal:{digest}"
                self.nodes[terminal_id] = RouteNode(
                    terminal_id, label, base_id=node.base_id or node_id,
                    role="terminal_candidate",
                )
                pair.append(terminal_id)
            if pair != [left, right]:
                self.terminal_pairs[(left, right)] = (pair[0], pair[1])

    def journey_nodes(self, origin_id: str, destination_id: str) -> list[str]:
        """Expand major service access using the research-linked endpoint pair."""
        origin, destination = self.node(origin_id), self.node(destination_id)
        pair = self.terminal_pairs.get((origin.base_id or origin_id, destination.base_id or destination_id))
        if pair is None:
            return [origin_id, destination_id]
        decision = self.mode_resolution(origin_id, destination_id)
        if decision.preferred_mode not in {TransportMode.TRAIN, TransportMode.FLIGHT,
                                           TransportMode.TRANSIT, TransportMode.BUS}:
            return [origin_id, destination_id]
        nodes = [origin_id, *pair, destination_id]
        return [item for index, item in enumerate(nodes) if not index or item != nodes[index - 1]]

    def node(self, node_id: str) -> RouteNode:
        return self.nodes[node_id]

    def for_day(self, date: str) -> "RouteResolver":
        """Share evidence/cache without sharing mutable per-day request context."""
        resolver = copy(self)
        resolver.departure_date = date
        return resolver

    def _departure_timestamp(self) -> str:
        local = datetime.fromisoformat(f"{self.departure_date}T{self.departure_time}")
        if not self.data.timezone:
            raise ValueError("Transit departure needs the trip timezone.")
        return local.replace(tzinfo=ZoneInfo(self.data.timezone)).astimezone(UTC).isoformat()

    @staticmethod
    def _haversine_km(first: Coordinates, second: Coordinates) -> float:
        lat1, lon1, lat2, lon2 = map(
            radians,
            (first.latitude, first.longitude, second.latitude, second.longitude),
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

    @staticmethod
    def cache_key(
        origin_id: str, destination_id: str, actual_mode: str
    ) -> tuple[str, str, str]:
        """Cache factual routes by actual returned mode, never by a preference."""
        return origin_id, destination_id, actual_mode.casefold()

    @staticmethod
    def _walking_limit(route: RouteFact) -> RouteFact:
        """Never turn an unavailable local mode into an impractical long walk."""
        limit = int(os.getenv("PLANNER_MAX_WALKING_MINUTES", "45"))
        if (
            route.mode == "walking"
            and route.duration_minutes is not None
            and route.duration_minutes > limit
        ):
            return route.model_copy(update={
                "duration_minutes": None,
                "status": "walking_threshold_exceeded",
                "provenance": "unknown",
                "diagnostic_route": route.model_dump(mode="json"),
            })
        return route

    def add_anchor(
        self,
        anchor_id: str,
        name: str,
        coordinates: Coordinates,
        *,
        base_id: str | None = None,
        region_id: str | None = None,
        country_code: str | None = None,
    ) -> None:
        self.nodes[anchor_id] = RouteNode(
            anchor_id,
            name,
            coordinates,
            base_id=base_id or anchor_id,
            region_id=region_id,
            country_code=country_code,
            role="base",
        )

    def movement_class(self, origin_id: str, destination_id: str):
        origin, destination = self.node(origin_id), self.node(destination_id)
        return classify_movement(
            TransportEndpoint(
                origin.id,
                origin.base_id,
                origin.region_id,
                origin.country_code,
                origin.role,
                origin.route_context,
            ),
            TransportEndpoint(
                destination.id,
                destination.base_id,
                destination.region_id,
                destination.country_code,
                destination.role,
                destination.route_context,
            ),
            international_trip_boundary=self.data.trip_scope == "international"
            and {origin.role, destination.role} & {"origin", "end"}
            and {origin.role, destination.role} & {"base"},
        )

    def provider_capabilities(self) -> set[str]:
        capabilities = set()
        if os.getenv("GOOGLE_MAPS_API_KEY") or os.getenv("GOOGLE_API_KEY"):
            capabilities.update({"DRIVE", "WALK", "BICYCLE", "TRANSIT"})
        if os.getenv("ORS_API_KEY"):
            capabilities.update({"DRIVE", "WALK", "BICYCLE"})
        return capabilities

    def mode_resolution(self, origin_id: str, destination_id: str) -> ModeResolution:
        decision = resolve_mode(
            self.movement_class(origin_id, destination_id),
            self.data.transport_preferences,
            self.provider_capabilities(),
        )
        # A generic preference list such as [flight, train] does not mean every
        # inter-base leg should fly. Prefer the user's allowed mode that has
        # explicit research evidence for this exact pair.
        researched_modes = {
            normalize_mode(route.mode)
            for (left, right, _), route in self.cache.items()
            if left == origin_id
            and right == destination_id
            and route.provenance.startswith("research_")
        }
        grounded_mode = next(
            (mode for mode in decision.requested_modes if mode in researched_modes),
            None,
        )
        if grounded_mode is None or grounded_mode == decision.preferred_mode:
            return decision
        preferences = dict(self.data.transport_preferences)
        key = (
            "local_modes"
            if decision.movement_class.value == "local"
            else "long_distance_modes"
        )
        preferences[key] = [grounded_mode.value] + [
            mode.value for mode in decision.requested_modes if mode != grounded_mode
        ]
        return resolve_mode(
            decision.movement_class,
            preferences,
            self.provider_capabilities(),
        )

    @staticmethod
    def _route_mode(mode: TransportMode | None) -> str:
        if mode == TransportMode.DRIVE:
            return "driving"
        if mode in {TransportMode.TAXI, TransportMode.RENTAL_CAR}:
            return mode.value
        if mode == TransportMode.WALK:
            return "walking"
        if mode == TransportMode.CYCLING:
            return "cycling"
        if mode in {
            TransportMode.TRAIN,
            TransportMode.METRO,
            TransportMode.BUS,
            TransportMode.TRANSIT,
        }:
            return "transit"
        return mode.value if mode else "unknown"

    @staticmethod
    def _research_decision(
        decision: ModeResolution, evidence: RouteFact
    ) -> ModeResolution | None:
        evidence_mode = normalize_mode(evidence.mode)
        # Provider routing may use DRIVE for a taxi/rental request, but the
        # itinerary retains the user-facing semantic mode.
        selected = decision.selected_mode or decision.preferred_mode
        matching = selected is not None and (
            evidence_mode == selected
            or (PROVIDER_MODE.get(evidence_mode) is not None
                and PROVIDER_MODE.get(evidence_mode) == PROVIDER_MODE.get(selected))
        )
        if matching:
            return decision.model_copy(update={"source": "research_evidence", "selected_mode": selected})
        return None

    async def resolve(self, origin_id: str, destination_id: str) -> RouteFact:
        decision = self.mode_resolution(origin_id, destination_id)
        request_key = (origin_id, destination_id, decision.selected_mode, self.departure_date, self.departure_time)
        if request_key in self.request_results:
            value = self.request_results[request_key]
            self._trace(value, "request_cache")
            return value
        allowed = [
            route
            for (a, b, mode_key), route in self.cache.items()
            if a == origin_id
            and b == destination_id
            and (route.scheduled_service is None
                 or route.scheduled_service.departure_at.date().isoformat() == self.departure_date)
            and (route.provenance.startswith("research_") or mode_key.endswith(f"@{self.departure_date}:{self.departure_time}") or "@" not in mode_key)
            and (self._research_decision(decision, route) is not None or (
                mode_key.endswith(f"@{self.departure_date}:{self.departure_time}")
                and route.mode_resolution is not None
                and route.mode_resolution.requested_modes == decision.requested_modes
                and route.mode_resolution.preferred_mode == decision.preferred_mode
            ))
        ]
        # A saved city-to-city duration is useful background evidence, but is
        # not a dated timetable.  For a selected, provider-typed terminal pair,
        # ask the existing transit provider once for the actual service rather
        # than presenting the estimate as a confirmed departure.
        needs_timetable = (
            decision.provider_mode == "TRANSIT"
            and self.node(origin_id).role == "terminal"
            and self.node(destination_id).role == "terminal"
        )
        if allowed and not (needs_timetable and not allowed[0].scheduled_service):
            value = self._walking_limit(allowed[0].model_copy(
                update={
                    "mode_resolution": self._research_decision(decision, allowed[0]) or allowed[0].mode_resolution
                }
            ))
            self._trace(value, "cache")
            return value
        if origin_id == destination_id:
            value = RouteFact(
                origin_id=origin_id,
                destination_id=destination_id,
                mode="same_anchor",
                duration_minutes=0,
                distance_km=0,
                straight_line_km=0,
                status="same_anchor",
                provenance="provider_verified",
                mode_resolution=decision,
            )
            self.cache[self.cache_key(origin_id, destination_id, "same_anchor")] = value
            self._trace(value, "same_anchor")
            return value
        evidence = [
            route
            for (a, b, _), route in self.cache.items()
            if a == origin_id
            and b == destination_id
            and route.provenance == "research_verified"
            and route.duration_minutes is not None
        ]
        if decision.selected_mode is None:
            value = RouteFact(
                origin_id=origin_id,
                destination_id=destination_id,
                mode=self._route_mode(decision.preferred_mode),
                duration_minutes=None,
                straight_line_km=0,
                status="unsupported_by_provider",
                provenance="unsupported",
                mode_resolution=decision,
                diagnostic_route=evidence[0].model_dump() if evidence else None,
            )
            value = self._local_planning_allowance(value, decision)
            self.request_results[request_key] = value
            self._trace(value, "unsupported_or_planning_allowance")
            return value
        value = self._walking_limit(await self._live(origin_id, destination_id, decision))
        value = self._local_planning_allowance(value, decision)
        # Cache the provider outcome for the duration of this Planner run,
        # including an explicit unresolved outcome.  Step 4 materializes many
        # trial allocations; retrying the same failed live leg in every trial
        # is neither new evidence nor useful work.  An unresolved cached value
        # still has ``duration_minutes=None`` and therefore remains an honest
        # scheduler failure, never a fabricated travel duration.
        #
        # The cache key continues to use the returned mode, never a preference.
        self.cache[self.cache_key(origin_id, destination_id, f"{value.mode}@{self.departure_date}:{self.departure_time}")] = value
        self.request_results[request_key] = value
        if value.duration_minutes is None and evidence:
            value = value.model_copy(
                update={"diagnostic_route": evidence[0].model_dump()}
            )
        self._trace(
            value, "live" if value.duration_minutes is not None else "unresolved"
        )
        return value

    def _local_planning_allowance(self, fact: RouteFact, decision: ModeResolution) -> RouteFact:
        """Offer a labelled local time allowance, never a verified route or service."""
        if fact.duration_minutes is not None or decision.movement_class.value != "local":
            return fact
        if self.data.travellers.get("accessibility_needs"):
            return fact
        origin, destination = self.node(fact.origin_id), self.node(fact.destination_id)
        if not origin.coordinates or not destination.coordinates:
            return fact
        distance = self._haversine_km(origin.coordinates, destination.coordinates)
        if not 0 < distance <= 25:
            return fact
        # These deliberately padded assumptions reserve time, not a factual route.
        allowances = {
            TransportMode.WALK: (3.5, 5), TransportMode.CYCLING: (10, 10),
            TransportMode.TRANSIT: (12, 20), TransportMode.METRO: (12, 20),
            TransportMode.BUS: (10, 20), TransportMode.TRAIN: (15, 20),
            TransportMode.DRIVE: (20, 15), TransportMode.TAXI: (20, 15),
            TransportMode.RENTAL_CAR: (20, 15),
        }
        for mode in decision.requested_modes:
            if mode not in allowances:
                continue
            speed, overhead = allowances[mode]
            minutes = int(ceil((distance * 1.5 / speed * 60 + overhead) / 5) * 5)
            if mode == TransportMode.WALK and minutes > int(os.getenv("PLANNER_MAX_WALKING_MINUTES", "45")):
                continue
            return fact.model_copy(update={
                "mode": self._route_mode(mode), "duration_minutes": minutes,
                "distance_km": None, "straight_line_km": distance,
                "status": "planning_allowance_unverified", "provenance": "unknown",
                "evidence_ids": [],
                "mode_resolution": decision.model_copy(update={
                    "selected_mode": mode, "selected_mode_allowed_by_user": True,
                    "source": "planning_allowance", "preference_satisfied": False,
                    "warnings": ["Estimated local travel allowance from coordinates; route existence, access and actual duration require confirmation."],
                }),
                "diagnostic_route": {"basis": "straight_line_distance_x1.5",
                                     "assumed_speed_kmh": speed, "overhead_minutes": overhead},
            })
        return fact

    @staticmethod
    def _direct_service(route: dict, origin: RouteNode, destination: RouteNode) -> ScheduledService | None:
        """Retain a direct service only when its stops match the requested terminals."""
        if origin.role != "terminal" or destination.role != "terminal":
            return None
        steps = [step for leg in route.get("legs", []) for step in leg.get("steps", [])]
        # Access walks and connecting services must not disappear into one train leg.
        if len(steps) != 1 or not steps[0].get("transitDetails"):
            return None
        details = steps[0]["transitDetails"]
        stops = details.get("stopDetails", {})
        if any(stops.get(key, {}).get("name", "").strip().casefold() != node.name.strip().casefold()
               for key, node in (("departureStop", origin), ("arrivalStop", destination))):
            return None
        try:
            localized = details.get("localizedValues", {})
            instants = {}
            for key in ("departureTime", "arrivalTime"):
                instant = datetime.fromisoformat(stops[key])
                if instant.tzinfo is None:
                    return None
                instants[key] = instant.astimezone(ZoneInfo(localized[key]["timeZone"]))
            identity = sha256(str((origin.id, destination.id, stops)).encode()).hexdigest()[:20]
            return ScheduledService(
                departure_at=instants["departureTime"], arrival_at=instants["arrivalTime"],
                evidence_ids=[f"google_routes_service:{identity}"],
            )
        except (KeyError, ValueError, TypeError, ZoneInfoNotFoundError):
            return None

    async def _live(
        self, origin_id: str, destination_id: str, decision: ModeResolution
    ) -> RouteFact:
        origin, destination = self.node(origin_id), self.node(destination_id)
        provider_mode = decision.provider_mode
        if not provider_mode:
            return RouteFact(
                origin_id=origin_id,
                destination_id=destination_id,
                mode=self._route_mode(decision.selected_mode),
                duration_minutes=None,
                straight_line_km=0,
                status="unsupported_by_provider",
                provenance="unsupported",
                mode_resolution=decision,
            )
        google_key = os.getenv("GOOGLE_MAPS_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if google_key:
            try:
                def waypoint(node: RouteNode) -> dict:
                    if node.coordinates:
                        return {"location": {"latLng": {
                            "latitude": node.coordinates.latitude,
                            "longitude": node.coordinates.longitude,
                        }}}
                    return {"address": node.name}

                request_body = {
                    "origin": waypoint(origin),
                    "destination": waypoint(destination),
                    "travelMode": provider_mode,
                }
                # Use this day's local departure, not 09:00 UTC on day one.
                # Out-of-window schedules must remain unknown, not today's data.
                if provider_mode == "TRANSIT":
                    departure = self._departure_timestamp()
                    offset = datetime.fromisoformat(departure) - datetime.now(UTC)
                    if not -7 <= offset.total_seconds() / 86400 <= 100:
                        raise ValueError("Transit date outside provider schedule window.")
                    request_body["departureTime"] = departure
                    request_body["transitPreferences"] = {"routingPreference": "LESS_WALKING"}
                async with httpx.AsyncClient(
                    timeout=float(os.getenv("ROUTING_HTTP_TIMEOUT_SECONDS", "8")),
                    trust_env=False,
                    event_hooks=self._http_hooks(),
                ) as client:
                    response = await client.post(
                        "https://routes.googleapis.com/directions/v2:computeRoutes",
                        headers={
                            "X-Goog-Api-Key": google_key,
                            "X-Goog-FieldMask": (
                                "routes.duration,routes.distanceMeters"
                                + (",routes.legs.steps.transitDetails" if provider_mode == "TRANSIT" else "")
                            ),
                        },
                        json=request_body,
                    )
                    response.raise_for_status()
                    routes = response.json().get("routes") or []
                    if not routes:
                        raise LookupError("Routes provider returned no route.")
                    route = routes[0]
                    seconds = int(str(route["duration"]).removesuffix("s"))
                    service = self._direct_service(route, origin, destination)
                    return RouteFact(
                        origin_id=origin_id,
                        destination_id=destination_id,
                        mode=self._route_mode(decision.selected_mode),
                        duration_minutes=service.elapsed_minutes if service else max(1, round(seconds / 60)),
                        scheduled_service=service,
                        distance_km=round(route.get("distanceMeters", 0) / 1000, 1),
                        straight_line_km=0,
                        status="verified",
                        provenance="provider_verified",
                        mode_resolution=decision,
                    )
            except RouteDispatchStopped:
                raise
            except Exception as error:
                # A failed transit request is never permission to request driving.
                # Keep provider responses and credentials out of logs, but make
                # the failure diagnosable by stable route IDs and mode.
                logger.warning(
                    "route_provider_result_unusable",
                    extra={
                        "origin_id": origin_id,
                        "destination_id": destination_id,
                        "provider_mode": provider_mode,
                        "error_type": type(error).__name__,
                        "status": "empty_routes" if isinstance(error, LookupError) else "request_unusable",
                        "departure_time": request_body.get("departureTime"),
                    },
                )
                pass
        ors_key = os.getenv("ORS_API_KEY")
        profile = {
            "DRIVE": "driving-car",
            "WALK": "foot-walking",
            "BICYCLE": "cycling-regular",
        }.get(provider_mode)
        if not ors_key or not profile:
            return await self._fallback_or_unsupported(
                origin_id, destination_id, decision
            )
        try:
            async with httpx.AsyncClient(
                timeout=float(os.getenv("ROUTING_HTTP_TIMEOUT_SECONDS", "8")),
                trust_env=False,
                event_hooks=self._http_hooks(),
            ) as client:
                first = await self._point(client, ors_key, origin)
                second = await self._point(client, ors_key, destination)
                response = await client.post(
                    f"https://api.openrouteservice.org/v2/directions/{profile}",
                    headers={
                        "Authorization": ors_key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "coordinates": [
                            [first.longitude, first.latitude],
                            [second.longitude, second.latitude],
                        ]
                    },
                )
                response.raise_for_status()
                summary = (response.json().get("routes") or [])[0].get("summary") or {}
                return RouteFact(
                    origin_id=origin_id,
                    destination_id=destination_id,
                    mode=self._route_mode(decision.selected_mode),
                    duration_minutes=max(1, round(float(summary["duration"]) / 60)),
                    distance_km=round(float(summary["distance"]) / 1000, 1),
                    straight_line_km=0,
                    status="verified",
                    provenance="provider_verified",
                    mode_resolution=decision,
                )
        except RouteDispatchStopped:
            raise
        except Exception:
            return await self._fallback_or_unsupported(
                origin_id, destination_id, decision
            )

    async def _fallback_or_unsupported(
        self, origin_id: str, destination_id: str, decision: ModeResolution
    ) -> RouteFact:
        """Retry only another explicit user-allowed mode after a live failure."""
        try:
            selected_index = decision.requested_modes.index(decision.selected_mode)
        except ValueError:
            selected_index = len(decision.requested_modes)
        supported = self.provider_capabilities()
        for alternative in decision.requested_modes[selected_index + 1 :]:
            provider_mode = PROVIDER_MODE.get(alternative)
            if (
                provider_mode
                and provider_mode in supported
                and provider_mode != decision.provider_mode
            ):
                fallback = decision.model_copy(
                    update={
                        "selected_mode": alternative,
                        "provider_mode": provider_mode,
                        "selection_status": ModeSelectionStatus.ALLOWED_ALTERNATIVE,
                        "fallback_used": True,
                        "fallback_reason": f"Preferred {decision.preferred_mode.value if decision.preferred_mode else 'mode'} routing failed; {alternative.value} is another user-allowed mode.",
                        "fallback_from": decision.preferred_mode,
                        "fallback_to": alternative,
                        "selected_mode_allowed_by_user": True,
                        "preference_satisfied": False,
                        "source": "provider_fallback",
                        "warnings": [
                            "A user-allowed fallback was used after the preferred routing request failed."
                        ],
                    }
                )
                return await self._live(origin_id, destination_id, fallback)
        unavailable = decision.model_copy(
            update={
                "selection_status": ModeSelectionStatus.UNAVAILABLE,
                "preference_satisfied": False,
                "source": "provider_runtime",
                "warnings": [
                    f"Configured provider could not return a {decision.provider_mode} route for this user-allowed mode."
                ],
            }
        )
        return RouteFact(
            origin_id=origin_id,
            destination_id=destination_id,
            mode=self._route_mode(decision.selected_mode),
            duration_minutes=None,
            straight_line_km=0,
            status="unknown",
            provenance="unknown",
            mode_resolution=unavailable,
        )

    def _http_hooks(self):
        hooks = {name: list(values) for name, values in HTTP_HOOKS.items()}
        if self.request_guard is not None:
            hooks.setdefault("request", []).insert(0, self.request_guard)
        return hooks

    async def _point(
        self, client: httpx.AsyncClient, key: str, node: RouteNode
    ) -> Coordinates:
        if node.coordinates:
            return node.coordinates
        response = await client.get(
            "https://api.openrouteservice.org/geocode/search",
            params={"text": node.name, "size": 1},
            headers={"Authorization": key},
        )
        response.raise_for_status()
        longitude, latitude = response.json()["features"][0]["geometry"]["coordinates"]
        return Coordinates(latitude=latitude, longitude=longitude)

    def _trace(self, route: RouteFact, source: str) -> None:
        self.trace.append(
            {
                "origin_id": route.origin_id,
                "destination_id": route.destination_id,
                "mode": route.mode,
                "duration_minutes": route.duration_minutes,
                "distance_km": route.distance_km,
                "status": route.status,
                "provenance": route.provenance,
                "mode_resolution": (
                    route.mode_resolution.model_dump(mode="json")
                    if route.mode_resolution
                    else None
                ),
                "resolution_source": source,
            }
        )
