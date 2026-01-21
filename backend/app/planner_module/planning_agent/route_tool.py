"""Planner-scoped route batches with per-HTTP-attempt budget enforcement."""
import asyncio
from dataclasses import replace
from datetime import date, time
from typing import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field

from ..models.planning import Coordinates, PlannerInput, RouteFact
from ..tools.routing import RouteResolver, RouteDispatchStopped
from .resources import ResourceGuard, ResourceLimit
from .contracts import fingerprint


class RoutePair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin_id: str
    destination_id: str


class RouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pairs: list[RoutePair] = Field(min_length=1, max_length=32)
    departure_date: date
    departure_time: time


class EvidenceRouteResolver(RouteResolver):
    def _local_planning_allowance(self, value, decision):
        # Tool data cannot silently turn missing routes into planning allowances.
        return value


class RouteDispatchDenied(RouteDispatchStopped):
    """An expected quota or duplicate denial, not a persistence failure."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def stay_anchor_id(stay):
    return "stay:" + fingerprint({key: stay.get(key) for key in ("stop_id", "name", "coordinates")})[:24]


class PlannerRouteTool:
    def __init__(self, data: PlannerInput, guard: ResourceGuard,
                 checkpoint: Callable[[], Awaitable[None]]):
        self.guard = guard
        self.checkpoint = checkpoint
        self.resolver = EvidenceRouteResolver(data, request_guard=self._before_request)
        bases = [node for node in self.resolver.nodes.values() if node.role == "base" and node.coordinates]
        for stay in data.stay_bases:
            if not stay.get("name") or not stay.get("coordinates"):
                continue
            coordinates = Coordinates.model_validate(stay["coordinates"])
            candidates = [(self.resolver._haversine_km(node.coordinates, coordinates), node.id) for node in bases]
            nearby = sorted(pair for pair in candidates if pair[0] <= self.resolver.local_attachment_km)
            base_id = stay.get("stop_id") if stay.get("stop_id") in {node.id for node in bases} else None
            if base_id is None and nearby and (len(nearby) == 1 or nearby[0][0] < nearby[1][0]):
                base_id = nearby[0][1]
            identity = stay_anchor_id(stay)
            self.resolver.add_anchor(identity, stay["name"], coordinates, base_id=base_id)
            self.resolver.nodes[identity] = replace(self.resolver.nodes[identity], role="stay_area")
        self.lock = asyncio.Lock()
        self.dispatched_requests: set[str] = set()

    def snapshot(self):
        return {"input_hash": fingerprint(self.resolver.data),
                "dispatched_requests": sorted(self.dispatched_requests),
                "cache": [[list(key), value.model_dump(mode="json")] for key, value in self.resolver.cache.items()],
                "request_results": [[list(key), value.model_dump(mode="json")] for key, value in self.resolver.request_results.items()]}

    def restore(self, saved):
        if saved["input_hash"] != fingerprint(self.resolver.data):
            raise ValueError("Saved route cache belongs to another input")
        maps = {}
        for name in ("cache", "request_results"):
            maps[name] = {}
            for key, raw in saved[name]:
                fact = RouteFact.model_validate(raw)
                if len(key) != (3 if name == "cache" else 5) or any(
                    identity not in self.resolver.nodes for identity in key[:2]
                ) or tuple(key[:2]) != (fact.origin_id, fact.destination_id):
                    raise ValueError("Saved route identity mismatch")
                maps[name][tuple(key)] = fact
        self.resolver.cache = maps["cache"]
        self.resolver.request_results = maps["request_results"]
        self.dispatched_requests = set(saved.get("dispatched_requests", []))

    async def _before_request(self, request):
        try:
            request_id = fingerprint({"method": request.method, "url": str(request.url),
                                      "body": request.content.decode("utf-8")})
            if request_id in self.dispatched_requests:
                raise RouteDispatchDenied("identical_route_request_not_repeated")
            timeout = await self.guard.reserve("route")
            self.dispatched_requests.add(request_id)
            await self.checkpoint()
            remaining = min(timeout, self.guard.remaining())
        except RouteDispatchDenied:
            raise
        except ResourceLimit as exc:
            raise RouteDispatchDenied("route_resource_limit") from exc
        except Exception as exc:
            raise RouteDispatchStopped("Route dispatch guard rejected request") from exc
        request.extensions["timeout"] = {key: remaining for key in ("connect", "read", "write", "pool")}

    async def __call__(self, arguments: dict):
        request = RouteRequest.model_validate(arguments)
        if request.departure_time.tzinfo is not None:
            raise ValueError("Use local trip time; offset is resolved by the registered trip timezone")
        if not self.resolver.data.start_date <= request.departure_date.isoformat() <= self.resolver.data.end_date:
            raise ValueError("Route date outside confirmed trip")
        for pair in request.pairs:
            if pair.origin_id not in self.resolver.nodes or pair.destination_id not in self.resolver.nodes:
                raise ValueError("Route endpoints must be registered")
        async with self.lock:
            self.guard.remaining()
            self.resolver.departure_date = request.departure_date.isoformat()
            self.resolver.departure_time = request.departure_time.isoformat()
            results = []
            unique = {}
            for pair in request.pairs:
                key = (pair.origin_id, pair.destination_id)
                if key not in unique:
                    try:
                        async with asyncio.timeout(self.guard.remaining()):
                            fact = await self.resolver.resolve(*key)
                    except RouteDispatchDenied as exc:
                        decision = self.resolver.mode_resolution(*key)
                        fact = RouteFact(
                            origin_id=pair.origin_id,
                            destination_id=pair.destination_id,
                            mode=self.resolver._route_mode(
                                decision.selected_mode or decision.preferred_mode
                            ),
                            duration_minutes=None,
                            straight_line_km=0,
                            status=(
                                "route_resource_limit"
                                if exc.reason == "route_resource_limit"
                                else "route_request_not_repeated"
                            ),
                            provenance="unknown",
                            mode_resolution=decision,
                            diagnostic_route={"reason": exc.reason},
                        )
                    unique[key] = fact.model_dump(mode="json")
                results.append(unique[key])
            return {"status": "ok", "routes": results,
                    "departure_date": request.departure_date.isoformat(),
                    "departure_time": request.departure_time.isoformat(),
                    "timing_note": "Requested date/time does not make static road estimates traffic-dependent."}
