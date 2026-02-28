"""Research operations and per-run dependencies used by the LangGraph nodes."""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections import defaultdict
from collections.abc import Callable
from datetime import date, timedelta
from itertools import pairwise
from time import perf_counter
from typing import Any

import httpx

from app.planner_module.models.research import (
    POI,
    Coordinates,
    DestinationCoverage,
    Evidence,
    GeographicCluster,
    MandatoryRequirement,
    ResearchCoverage,
    ResearchGap,
    ResearchInput,
    ResearchQuality,
    ResolvedStop,
    RouteEvidence,
    StayBase,
    TransportRequirement,
    WeatherContext,
)
from app.planner_module.providers import ResearchProvider
from app.planner_module.reasoner import ResearchReasoner
from app.planner_module.tools.scheduling import opening_bounds
from app.planner_module.utils.currency import destination_currency
from app.planner_module.utils.services import (
    Cache,
    PersistentResearchStore,
    PricingConfig,
    ProviderBudget,
    ResearchLimits,
)

logger = logging.getLogger(__name__)

BASE_LIKE_STOP_TYPES = frozenset(
    {
        "locality",
        "sublocality",
        "administrative_area_level_1",
        "administrative_area_level_2",
        "city",
        "region",
        # Internal roles assigned after provider resolution.
        "base",
        "mandatory_base",
    }
)


class ResearchTools:
    """Provider-backed operations; orchestration lives exclusively in langgraph.py."""

    def __init__(
        self,
        provider: ResearchProvider,
        *,
        reasoner: ResearchReasoner | None = None,
        limits: ResearchLimits | None = None,
        pricing: PricingConfig | None = None,
        stage_observer: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.provider = provider
        self.reasoner = reasoner
        self.limits = limits or ResearchLimits()
        persistent = PersistentResearchStore(self.limits.persistent_store_path)
        self.cache = Cache(persistent=persistent)
        self.budget = ProviderBudget(self.limits, self.cache.metrics, persistent)
        self.pricing = pricing or PricingConfig()
        self.stage_observer = stage_observer or (lambda _name, _value: None)
        self.evidence: list[Evidence] = []
        self.route_expected_member_count = 0
        self.requirements: list[MandatoryRequirement] = []
        self.requirement_stop_ids: dict[str, set[str]] = defaultdict(set)
        self.discovery_queries = 0
        self.enrichment_attempts: set[tuple[str, str]] = set()
        self.optional_enrichment_attempts: set[tuple[str, str]] = set()
        self.semaphore = asyncio.Semaphore(self.limits.max_concurrent_requests)
        configure_limits = getattr(provider, "configure_limits", None)
        if callable(configure_limits):
            configure_limits(self.limits)

    def write_stage(self, name: str, value: dict[str, Any]) -> None:
        if name == "09_quality_gate":
            logger.log(
                logging.WARNING if value["status"] != "complete" else logging.INFO,
                "quality_evaluated",
                extra={
                    "status": value["status"],
                    "critical_gaps": len(value["critical_gaps"]),
                    "optional_gaps": len(value["optional_gaps"]),
                },
            )
        if name == "10_repair":
            logger.warning("destination_repair_performed")
        logger.debug(
            "research_snapshot",
            extra={
                "cache_hits": self.cache.metrics.cache_hits,
                "cache_misses": self.cache.metrics.cache_misses,
            },
        )
        for key, items in value.items():
            if isinstance(items, (dict, list)):
                logger.debug(
                    "stage_collection", extra={"collection": key, "count": len(items)}
                )
        self.stage_observer(name, value)

    def _evidence(
        self, provider: str, subject: str, value: dict[str, Any], kind: str = "provider"
    ) -> str:
        evidence_id = f"ev_{len(self.evidence) + 1:03d}"
        self.evidence.append(
            Evidence(
                id=evidence_id,
                kind=kind,
                provider=provider,
                subject=subject,
                retrieved_value=value,
            )
        )
        return evidence_id

    async def _call(
        self,
        provider_name: str,
        maximum: int,
        cache_key: str,
        ttl: int,
        operation,
        *,
        priority: str = "core",
    ):
        async def counted():
            # Exactly one retry is allowed for an interrupted HTTP request. Each
            # attempt consumes budget before it is sent; a budget failure is a
            # visible research gap, never an unbounded retry loop.
            for attempt in range(2):
                self.budget.count(provider_name, maximum)
                by_priority = (
                    self.cache.metrics.provider_requests_by_priority.setdefault(
                        priority, {}
                    )
                )
                by_priority[provider_name] = by_priority.get(provider_name, 0) + 1
                try:
                    async with self.semaphore:
                        started = perf_counter()
                        logger.debug(
                            "provider_operation_started",
                            extra={"provider": provider_name, "attempt": attempt + 1},
                        )
                        result = await asyncio.wait_for(
                            operation(), timeout=self.limits.node_timeout_seconds
                        )
                        logger.debug(
                            "provider_operation_completed",
                            extra={
                                "provider": provider_name,
                                "duration_ms": round(
                                    (perf_counter() - started) * 1000, 2
                                ),
                            },
                        )
                        if result is None or result == []:
                            logger.warning(
                                "provider_result_unavailable",
                                extra={"provider": provider_name},
                            )
                        return result
                except (httpx.TransportError, TimeoutError):
                    logger.warning(
                        "provider_transport_failure",
                        extra={"provider": provider_name, "attempt": attempt + 1},
                    )
                    if attempt == 1:
                        raise
                except Exception:
                    logger.exception(
                        "provider_operation_failed",
                        extra={"provider": provider_name},
                    )
                    raise

        return await self.cache.get_or_set(cache_key.lower().strip(), ttl, counted)

    def merge_metrics(self) -> None:
        """A live provider may make several HTTP calls inside one logical node."""
        raw_provider_counts = getattr(self.provider, "provider_metrics", None)
        if isinstance(raw_provider_counts, dict):
            self.cache.metrics.provider_requests = dict(raw_provider_counts)
        raw_llm = getattr(self.reasoner, "metrics", None)
        if isinstance(raw_llm, dict):
            self.cache.metrics.llm_calls = int(raw_llm.get("calls", 0))
            self.cache.metrics.llm_input_tokens = int(raw_llm.get("input_tokens", 0))
            self.cache.metrics.llm_output_tokens = int(raw_llm.get("output_tokens", 0))
            self.cache.metrics.llm_calls_by_model = dict(
                raw_llm.get("calls_by_model", {})
            )
            self.cache.metrics.llm_input_tokens_by_model = dict(
                raw_llm.get("input_tokens_by_model", {})
            )
            self.cache.metrics.llm_output_tokens_by_model = dict(
                raw_llm.get("output_tokens_by_model", {})
            )
        self.cache.metrics.estimated_cost_usd = self.pricing.estimate(
            self.cache.metrics
        )

    def _required_member_count(self, trip: ResearchInput) -> int:
        """Route size comes from researched candidate members, never only user wording."""
        if trip.destination.type == "route" and self.route_expected_member_count:
            return self.route_expected_member_count
        return max(
            [int(value) for value in re.findall(r"\b(\d+)\b", " ".join(trip.must_see))]
            or [0]
        )

    @staticmethod
    def _source_member_count(web_results: list[dict[str, Any]]) -> int:
        """Extract an explicitly stated route size from source text, never user prose."""
        words = {
            "one": 1,
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5,
            "six": 6,
            "seven": 7,
            "eight": 8,
            "nine": 9,
            "ten": 10,
            "twelve": 12,
        }
        text = " ".join(str(item.get("content", "")) for item in web_results).casefold()
        values = [
            int(value)
            for value in re.findall(
                r"\b(\d+)(?:\s+\w+){0,3}\s+(?:members|temples|stops|sites)\b", text
            )
        ]
        values += [
            number
            for word, number in words.items()
            if re.search(
                rf"\b{word}(?:\s+\w+){{0,3}}\s+(?:members|temples|stops|sites)\b", text
            )
        ]
        return max(values, default=0)

    def _requirements_for(self, trip: ResearchInput) -> list[MandatoryRequirement]:
        """Classify promises once; quality checks must not use POI name substrings."""
        if self.requirements:
            return self.requirements
        items = []
        destination_names = {item.casefold() for item in trip.destination.items}
        for index, text in enumerate(trip.must_see, 1):
            normalized = text.casefold()
            if trip.destination.type == "route" and "all" in normalized:
                kind = "all_route_members"
            elif normalized in destination_names:
                kind = "destination_or_base"
            elif any(
                word in normalized for word in ("experience", "viewpoint", "area")
            ):
                kind = "poi_or_experience"
            else:
                kind = "exact_poi"
            items.append(
                MandatoryRequirement(
                    id=f"req_{index}", requirement_text=text, requirement_type=kind
                )
            )
        self.requirements = items
        return items

    @staticmethod
    def _is_base_like_stop(stop: ResolvedStop | dict[str, Any]) -> bool:
        """Use provider place semantics to distinguish city bases from visitable stops."""
        if isinstance(stop, ResolvedStop):
            return stop.type in BASE_LIKE_STOP_TYPES
        stop_type = stop.get("type")
        categories = stop.get("categories", [])
        return stop_type in BASE_LIKE_STOP_TYPES or bool(
            BASE_LIKE_STOP_TYPES.intersection(categories or [])
        )

    @staticmethod
    def _trip_context(trip: ResearchInput) -> dict[str, Any]:
        return {
            "origin": trip.origin,
            "end_location": trip.end_location,
            "purpose": trip.trip_purpose,
            "must_see": trip.must_see,
            "activity_interests": trip.activity_interests,
            "avoid": trip.avoid,
            "travel_style": trip.travel_style,
            "travellers": trip.travellers.model_dump(),
            "accessibility_requirements": trip.travellers.accessibility_needs,
            "local_transport_preferences": trip.transport_preferences.local_modes,
            "long_distance_transport_preferences": trip.transport_preferences.long_distance_modes,
            "day_start": trip.time_preferences.preferred_day_start,
            "day_end": trip.time_preferences.preferred_day_end,
            "budget": trip.budget.model_dump(),
            "trip_context_notes": trip.trip_context_notes,
            "traveler_context": trip.traveler_context,
        }

    @staticmethod
    def _category_buckets(categories: list[str], name: str = "") -> list[str]:
        """Generic provider-category normalization for diversity, never trip-specific."""
        text = " ".join([*categories, name]).casefold()
        rules = {
            "food": ("restaurant", "cafe", "meal_", "food", "bakery"),
            "nightlife": ("bar", "night_club", "nightclub", "pub"),
            "beach_coast": ("beach", "coast", "marina"),
            "nature": ("park", "natural", "garden", "forest", "waterfall", "bamboo"),
            "heritage_culture": (
                "museum",
                "historic",
                "heritage",
                "fort",
                "castle",
                "art_gallery",
                "monument",
                "cultural_landmark",
                "historical_landmark",
                "palace",
            ),
            "religious": ("church", "temple", "shrine", "place_of_worship"),
            "shopping": ("market", "shopping", "store"),
            "viewpoint": ("viewpoint", "observation", "lookout"),
            "entertainment": ("amusement", "aquarium", "zoo", "anime"),
        }
        values = [
            bucket
            for bucket, words in rules.items()
            if any(word in text for word in words)
        ]
        return values or ["other"]

    async def resolve_destinations(
        self, trip: ResearchInput, *, repair: bool = False
    ) -> list[ResolvedStop]:
        requirements = self._requirements_for(trip)
        names = list(trip.destination.items)
        source_ids: list[str] = []
        if trip.destination.type in {"country", "region", "route", "open"}:
            if trip.destination.type == "route":
                query_prefix = (
                    "targeted repair: exact missing member names and localities"
                    if repair
                    else "authoritative definition: exact complete member list and localities"
                )
            else:
                query_prefix = "source-backed useful areas and bases"
            query = f"{query_prefix} for {trip.destination.type} {', '.join(trip.destination.items)}"
            web_results = await self._call(
                "web_search",
                self.limits.max_web_searches,
                f"destination-web:{query}",
                3600,
                lambda: self.provider.web_search(query),
            )
            source_ids = [
                self._evidence(
                    "tavily", item.get("title", "destination web result"), item, "web"
                )
                for item in web_results
            ]
            # Broad destinations and thematic circuits must be grounded in sources;
            # do not let an LLM fill an empty web result from model memory.
            if self.reasoner and web_results:
                if self.reasoner.metrics["calls"] >= self.limits.max_llm_calls:
                    return []
                try:
                    names = await self.reasoner.select_destination_names(
                        destination_type=trip.destination.type,
                        requested_name=trip.destination.items[0],
                        duration_days=trip.duration_days,
                        web_results=web_results,
                        trip_context=self._trip_context(trip),
                    )
                except Exception:
                    logger.warning("destination_reasoning_unavailable", exc_info=True)
                    names = []
            # Explicit mandatory requirements may still be resolvable even when
            # broad area decomposition has no web result or semantic selection.
            if not names:
                names = []
            if trip.destination.type in {"country", "region"}:
                # The semantic model may return many plausible areas. Keep a
                # planner-usable number of bases proportional to trip length;
                # explicit mandatory requirements are resolved separately below.
                names = names[: max(2, min(4, math.ceil(trip.duration_days / 2)))]
            if trip.destination.type == "route":
                self.route_expected_member_count = max(
                    self.route_expected_member_count,
                    self._source_member_count(web_results),
                    len(names),
                )
        tasks = [
            self._call(
                "google_text_search",
                self.limits.max_google_searches,
                f"resolve:{trip.destination.type}:{item}",
                86400,
                lambda item=item: self.provider.resolve_destination(
                    trip.destination.type, item, trip.destination.subtype
                ),
            )
            for item in names
        ]
        records = [
            record
            for result in await asyncio.gather(*tasks, return_exceptions=True)
            if not isinstance(result, Exception)
            for record in result
        ]
        # Provider place semantics, not text length, decide whether a broad-trip
        # must-see is a destination base. Natural landmarks remain POI/experience
        # requirements and are researched as visitable places.
        for requirement in requirements:
            matched = next(
                (
                    record
                    for record in records
                    if requirement.requirement_text.casefold()
                    in record["name"].casefold()
                ),
                None,
            )
            is_base = bool(
                matched and self._is_base_like_stop(matched)
            )
            if requirement.requirement_type == "exact_poi" and is_base:
                requirement.requirement_type = "destination_or_base"
        unique: dict[str, dict[str, Any]] = {record["id"]: record for record in records}
        stops: list[ResolvedStop] = []
        for record in unique.values():
            evidence_id = self._evidence(
                record.get("provider", "destination_provider"), record["name"], record
            )
            stop_type = record.get("type", "city")
            if trip.destination.type in {"city", "cities"}:
                stop_type = "city"
            elif trip.destination.type in {"country", "region"} and self._is_base_like_stop(record):
                stop_type = "base"
            stops.append(
                ResolvedStop(
                    id=record["id"],
                    google_place_id=record.get("google_place_id"),
                    name=record["name"],
                    type=stop_type,
                    coordinates=Coordinates(**record["coordinates"]),
                    address=record.get("address"),
                    rationale=record.get("rationale", "Provider-resolved destination"),
                    confidence=record.get("confidence", "high"),
                    evidence_ids=[evidence_id, *source_ids],
                )
            )
        # Resolve explicit must-sees before generic attraction discovery.  They are
        # retained as direct POIs and cannot be displaced by provider ordering.
        exact = [
            item
            for item in requirements
            if item.requirement_type in {"exact_poi", "poi_or_experience"}
        ]
        base = [
            item
            for item in requirements
            if item.requirement_type == "destination_or_base"
        ]
        for requirement in [*base, *exact]:
            kind = (
                "city"
                if requirement.requirement_type == "destination_or_base"
                else "poi"
            )
            try:
                records = await self._call(
                    "google_text_search",
                    self.limits.max_google_searches,
                    f"mandatory:{kind}:{requirement.requirement_text}",
                    86400,
                    lambda requirement=requirement, kind=kind: (
                        self.provider.resolve_destination(
                            kind, requirement.requirement_text, None
                        )
                    ),
                    priority="mandatory",
                )
            except Exception:
                logger.warning(
                    "mandatory_destination_lookup_unavailable",
                    extra={"requirement_id": requirement.id},
                    exc_info=True,
                )
                records = []
            for record in records[:1]:
                self.requirement_stop_ids[requirement.id].add(record["id"])
                existing = next(
                    (stop for stop in stops if stop.id == record["id"]), None
                )
                if existing:
                    # A provider identity can occur both in broad base selection
                    # and exact mandatory resolution. Preserve its semantic role.
                    if kind == "poi":
                        stops[stops.index(existing)] = existing.model_copy(
                            update={
                                "type": "mandatory_poi",
                                "rationale": "Explicit mandatory POI/experience resolved before optional research.",
                            }
                        )
                    continue
                evidence_id = self._evidence(
                    record.get("provider", "google_places"), record["name"], record
                )
                stops.append(
                    ResolvedStop(
                        id=record["id"],
                        google_place_id=record.get("google_place_id"),
                        name=record["name"],
                        type="mandatory_poi" if kind == "poi" else "mandatory_base",
                        coordinates=Coordinates(**record["coordinates"]),
                        address=record.get("address"),
                        rationale="Explicit mandatory requirement resolved before optional research.",
                        confidence="high",
                        evidence_ids=[evidence_id],
                    )
                )
        # A route is not usable until the requested member set is complete.
        # Returning the partial result lets the bounded graph repair identify it.
        return stops

    async def repair_destination_resolution(
        self, trip: ResearchInput
    ) -> list[ResolvedStop]:
        """One targeted retry with a more explicit query; callers bound the pass count."""
        if trip.destination.type == "route":
            return await self.resolve_destinations(trip, repair=True)
        if trip.destination.type not in {"city", "cities", "poi"}:
            return []
        repaired = trip.model_copy(deep=True)
        repaired.destination.items = [
            f"{item}, {trip.origin}" for item in trip.destination.items
        ]
        return await self.resolve_destinations(repaired)

    async def research_context(
        self, trip: ResearchInput, stops: list[ResolvedStop]
    ) -> tuple[list[WeatherContext], dict[str, Any]]:
        async def one_weather(stop: ResolvedStop) -> WeatherContext:
            raw = await self._call(
                "weather",
                self.limits.max_weather_requests,
                f"weather:{stop.id}:{trip.start_date}:{trip.end_date}",
                21600,
                lambda: self.provider.weather(
                    stop.model_dump(), str(trip.start_date), str(trip.end_date)
                ),
            )
            evidence_id = self._evidence(
                raw.get("provider", "weather"),
                stop.name,
                raw,
                "seasonal" if raw.get("kind") == "seasonal" else "provider",
            )
            return WeatherContext(
                stop_id=stop.id,
                evidence_type=raw.get("kind", "unknown"),
                summary=raw.get("summary", "Weather unavailable"),
                evidence_ids=[evidence_id],
            )

        # Nearby circuit stops share one weather request; this is weather context,
        # not eight falsely independent forecasts for the same small area.
        unique_weather_stops: dict[tuple[float, float], ResolvedStop] = {}
        for stop in stops:
            unique_weather_stops.setdefault(
                (
                    round(stop.coordinates.latitude, 1),
                    round(stop.coordinates.longitude, 1),
                ),
                stop,
            )
        weather_results = (
            await asyncio.gather(
                *(one_weather(stop) for stop in unique_weather_stops.values()),
                return_exceptions=True,
            )
            if stops
            else []
        )
        weather = [
            item
            if isinstance(item, WeatherContext)
            else WeatherContext(
                stop_id=stop.id,
                evidence_type="unknown",
                summary="Weather context unavailable; verify closer to travel.",
            )
            for stop, item in zip(unique_weather_stops.values(), weather_results)
        ]
        local_currency = (
            destination_currency(trip.destination.items, trip.destination.type)
            or trip.currency_code.upper()
        )
        if (
            local_currency == trip.currency_code.upper()
            or trip.trip_scope == "domestic"
        ):
            return list(weather), {
                "base_currency": trip.currency_code.upper(),
                "quote_currency": local_currency,
                "conversion_required": False,
                "status": "not_required",
                "reason": "Home and destination currencies are the same; no exchange-rate request is useful.",
                "source_ids": [],
            }
        try:
            raw_currency = await self._call(
                "currency",
                2,
                f"currency:{trip.currency_code}:{local_currency}",
                43200,
                lambda: self.provider.currency(trip.currency_code, local_currency),
            )
        except Exception:
            logger.warning("currency_context_unavailable", exc_info=True)
            return list(weather), {
                "base_currency": trip.currency_code,
                "quote_currency": local_currency,
                "conversion_required": True,
                "status": "unavailable",
                "rate": None,
                "source_ids": [],
                "unresolved": "Currency provider unavailable.",
            }
        if not isinstance(raw_currency, dict):
            raw_currency = {"rate": None, "status": "unavailable"}
        currency_evidence = self._evidence(
            raw_currency.get("provider", "currency"),
            f"{trip.currency_code}/{local_currency}",
            raw_currency,
        )
        return list(weather), {
            "base_currency": trip.currency_code,
            "quote_currency": local_currency,
            "conversion_required": True,
            "status": "verified_rate"
            if raw_currency.get("rate") is not None
            else "unavailable",
            "rate": raw_currency.get("rate"),
            "source_ids": [currency_evidence],
        }

    async def discover_pois(
        self,
        trip: ResearchInput,
        stops: list[ResolvedStop],
        *,
        stop_ids: set[str] | None = None,
        repair: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        # A route member or explicit POI is already a visitable target. Promote it
        # directly instead of paying for a generic "attractions inside" search.
        primary_stops = [
            stop
            for stop in stops
            if stop.google_place_id
            and (
                trip.destination.type == "poi"
                or stop.type == "mandatory_poi"
                or (
                    trip.destination.type == "route"
                    and not self._is_base_like_stop(stop)
                )
            )
        ]
        direct = {}
        for stop in primary_stops:
            requirement_ids = [
                req_id
                for req_id, stop_ids in self.requirement_stop_ids.items()
                if stop.id in stop_ids
            ]
            direct[stop.id] = [
                {
                    "id": stop.id,
                    "google_place_id": stop.google_place_id,
                    "name": stop.name,
                    "coordinates": stop.coordinates.model_dump(),
                    "address": stop.address,
                    "categories": [stop.type],
                    "priority": "must",
                    "is_mandatory": True,
                    "requirement_ids": requirement_ids,
                    "selection_reason": (
                        "explicit_must_see"
                        if requirement_ids
                        else "explicit_requested_destination"
                    ),
                    "relevance": "Explicit requested destination, route member, or mandatory POI.",
                    "provider": "google_places",
                }
            ]
        remaining = [
            stop
            for stop in stops
            if stop.id not in direct and (stop_ids is None or stop.id in stop_ids)
        ]
        interest_queries = list(
            dict.fromkeys(
                [
                    *trip.activity_interests,
                    *([trip.trip_purpose] if trip.trip_purpose else []),
                    *trip.must_see,
                ]
            )
        )
        theme_categories = {
            category
            for theme in trip.activity_interests
            for category in self._category_buckets([], theme)
        }
        if len(interest_queries) < 3 or len(theme_categories - {"other"}) == 1:
            fallback = (
                "scenic viewpoints and easy nature visits"
                if trip.destination.type in {"region", "poi"}
                else "culture nature and viewpoints"
            )
            interest_queries.extend(["top visitor attractions", fallback])
        # A single blended query often lets Google return only one commercial
        # category. Split a bounded set of stated interests across bases, then
        # deduplicate before shortlist scoring.
        if repair:
            # Put the repair query first so the bounded slice cannot discard it.
            interest_queries = ["backup visitor attractions", *interest_queries]
        available_queries = max(
            0, self.limits.max_discovery_queries_total - self.discovery_queries
        )
        if not repair and self.limits.max_gap_repair_passes:
            available_queries -= min(2, max(0, available_queries - len(remaining)))
        queries_by_stop = {stop.id: [] for stop in remaining}
        for query in interest_queries:
            for stop in remaining:
                if available_queries > 0:
                    queries_by_stop[stop.id].append(query)
                    available_queries -= 1
        self.discovery_queries += sum(map(len, queries_by_stop.values()))

        async def one(stop: ResolvedStop):
            results = await asyncio.gather(
                *(
                    self._call(
                        "google_text_search",
                        self.limits.max_google_searches,
                        f"discover:{'repair:' if repair else ''}{stop.id}:{query}",
                        86400,
                        lambda query=query: self.provider.discover_pois(
                            stop.model_dump(), query
                        ),
                        priority="optional",
                    )
                    for query in queries_by_stop[stop.id]
                ),
                return_exceptions=True,
            )
            records = []
            for query, batch in zip(queries_by_stop[stop.id], results):
                if isinstance(batch, list):
                    for item in batch:
                        if not isinstance(item, dict):
                            logger.warning("invalid_discovery_candidate")
                            continue
                        record = dict(item)
                        record["discovery_themes"] = [query]
                        record["repair_candidate"] = repair
                        records.append(record)
            return stop.id, records

        return {
            **direct,
            **dict(await asyncio.gather(*(one(stop) for stop in remaining))),
        }

    def _candidate_rank(
        self, record: dict[str, Any], trip: ResearchInput | None = None
    ) -> tuple:
        categories = record.get("categories", [])
        text = " ".join(
            [record["name"], *categories, *record.get("discovery_themes", [])]
        ).casefold()
        interests = [*trip.activity_interests, trip.trip_purpose or ""] if trip else []
        visitor = any(
            term in category
            for category in categories
            for term in (
                "tourist_attraction",
                "museum",
                "landmark",
                "monument",
                "park",
                "temple",
                "shrine",
                "art_gallery",
            )
        )
        return (
            not record.get("repair_candidate", False),
            not visitor,
            -sum(bool(term.strip()) and term.casefold() in text for term in interests),
            not bool(record.get("google_place_id")),
        )

    def canonical_candidates(
        self, records, previous=(), *, trip=None, reserve_repair=False
    ):
        """Bound new admissions without evicting earlier research identities."""
        merged = [dict(item) for item in previous]
        additions = []
        for item in records:
            if not isinstance(item, dict):
                logger.warning("invalid_discovery_candidate")
                continue
            try:
                if not item.get("id") or not item.get("name", "").strip():
                    continue
                Coordinates(**item["coordinates"])
            except (KeyError, TypeError, ValueError):
                logger.warning("invalid_discovery_candidate")
                continue
            categories = item.get("categories", [])
            if (
                not (item.get("is_mandatory") or item.get("priority") == "must")
                and any(
                    category in {"lodging", "hotel", "motel", "hostel"}
                    for category in categories
                )
                and not any(
                    category in {"tourist_attraction", "museum", "historical_landmark"}
                    for category in categories
                )
            ):
                continue
            existing = next(
                (
                    old
                    for old in merged + additions
                    if (
                        item.get("google_place_id")
                        and item.get("google_place_id") == old.get("google_place_id")
                    )
                    or (
                        not (item.get("google_place_id") and old.get("google_place_id"))
                        and re.sub(r"\W+", "", item["name"].casefold())
                        == re.sub(r"\W+", "", old["name"].casefold())
                        and item.get("coordinates")
                        and old.get("coordinates")
                        and _haversine(
                            Coordinates(**item["coordinates"]),
                            Coordinates(**old["coordinates"]),
                        )
                        <= 0.05
                    )
                ),
                None,
            )
            if existing is None:
                additions.append(dict(item))
                continue
            for field, value in item.items():
                if isinstance(value, list):
                    existing[field] = list(existing.get(field) or [])
                    for member in value:
                        if member not in existing[field]:
                            existing[field].append(member)
                elif field == "is_recommendable":
                    existing[field] = existing.get(field, True) and value
                elif field in {"is_mandatory", "repair_candidate"}:
                    existing[field] = bool(existing.get(field) or value)
                elif field == "priority" and value == "must":
                    existing[field] = value
                elif field == "business_status" and value in {
                    "CLOSED_PERMANENTLY",
                    "CLOSED_TEMPORARILY",
                }:
                    existing[field] = value
                elif existing.get(field) in (None, "", "unknown") and value is not None:
                    existing[field] = value
        buckets = defaultdict(list)
        for item in additions:
            if item.get("is_mandatory") or item.get("priority") == "must":
                merged.append(item)
            else:
                category = self._category_buckets(
                    item.get("categories", []), item["name"]
                )[0]
                buckets[category].append(item)
        for bucket in buckets.values():
            bucket.sort(key=lambda item: self._candidate_rank(item, trip))
        available = max(0, self.limits.max_poi_candidates_per_stop - len(merged))
        if reserve_repair and self.limits.max_gap_repair_passes > 0:
            available = max(0, available - min(2, available // 4))
        while available and any(buckets.values()):
            for bucket in buckets.values():
                if bucket and available:
                    merged.append(bucket.pop(0))
                    available -= 1
        return merged

    def candidate_pois(
        self, trip: ResearchInput, candidates: dict[str, list[dict[str, Any]]]
    ) -> dict[str, list[POI]]:
        """Keep provider identities as truthful backups without paid enrichment."""
        result = {}
        for stop_id, records in candidates.items():
            result[stop_id] = []
            for record in records:
                try:
                    poi = POI(
                        id=record["id"],
                        google_place_id=record.get("google_place_id"),
                        name=record["name"],
                        stop_id=stop_id,
                        coordinates=Coordinates(**record["coordinates"]),
                        address=record.get("address"),
                        categories=record.get("categories", []),
                        normalized_categories=self._category_buckets(
                            record.get("categories", []), record["name"]
                        ),
                        priority=record.get("priority", "normal"),
                        is_mandatory=record.get("is_mandatory", False),
                        requirement_ids=record.get("requirement_ids", []),
                        relevance=record.get("relevance", "Provider-discovered backup"),
                        selection_reason=record.get("selection_reason")
                        or (
                            "Discovery themes: "
                            + ", ".join(record.get("discovery_themes", []))
                        ),
                        image={
                            "provider": "google_places",
                            "place_id": record["google_place_id"],
                            "resolution_strategy": "runtime_place_photo",
                        }
                        if record.get("google_place_id")
                        else None,
                        evidence_ids=[
                            self._evidence(
                                record.get("provider", "poi_candidate"),
                                record["name"],
                                record,
                            )
                        ],
                        unresolved_fields=[
                            "opening_hours",
                            "ticket_cost",
                            "reservation_rules",
                            "accessibility",
                        ],
                    )
                except (KeyError, ValueError, TypeError):
                    continue
                known = {
                    field: value
                    for field, value in record.items()
                    if field in POI.model_fields
                    and value is not None
                    and field
                    not in {
                        "id",
                        "stop_id",
                        "coordinates",
                        "evidence_ids",
                        "unresolved_fields",
                    }
                }
                data = poi.model_dump()
                data.update(known)
                themes = record.get("discovery_themes", [])
                if themes:
                    data["selection_reason"] = "; ".join(
                        filter(
                            None,
                            [
                                record.get("selection_reason"),
                                "Discovery themes: " + ", ".join(themes),
                            ],
                        )
                    )
                data["evidence_ids"] = list(
                    dict.fromkeys(poi.evidence_ids + record.get("evidence_ids", []))
                )
                text = " ".join(
                    [record["name"], *record.get("categories", [])]
                ).casefold()
                conflicts = [
                    term
                    for term in trip.avoid
                    if term.strip() and term.casefold() in text
                ]
                if conflicts:
                    data["is_recommendable"] = False
                    data["restrictions"] = list(
                        dict.fromkeys(
                            data.get("restrictions", [])
                            + [
                                "Conflicts with avoid preference: " + term
                                for term in conflicts
                            ]
                        )
                    )
                try:
                    result[stop_id].append(
                        self._duration_fallback(trip, POI.model_validate(data))
                    )
                except (ValueError, TypeError):
                    logger.warning(
                        "invalid_discovery_candidate", extra={"stop_id": stop_id}
                    )
                    safe = poi.model_copy(
                        update={
                            "is_recommendable": not bool(conflicts)
                            and poi.is_recommendable
                        }
                    )
                    result[stop_id].append(self._duration_fallback(trip, safe))
        return result

    @staticmethod
    def merge_pois(*pools: dict[str, list[POI]]) -> dict[str, list[POI]]:
        """Overlay known facts while retaining earlier identities and evidence."""
        result = {}
        for pool in pools:
            for stop_id, items in pool.items():
                bucket = result.setdefault(stop_id, {})
                for poi in items:
                    key = poi.google_place_id or poi.id
                    previous = bucket.get(key)
                    if previous is None:
                        previous = next(
                            (
                                old
                                for old in bucket.values()
                                if old.id == poi.id
                                or (
                                    not (old.google_place_id and poi.google_place_id)
                                    and ResearchTools._normalized_poi_name(old)
                                    == ResearchTools._normalized_poi_name(poi)
                                    and _haversine(old.coordinates, poi.coordinates)
                                    <= 0.05
                                )
                            ),
                            None,
                        )
                        if previous is not None:
                            key = next(
                                identity
                                for identity, value in bucket.items()
                                if value is previous
                            )
                    if previous is None:
                        bucket[key] = poi
                        continue
                    data = previous.model_dump()
                    incoming = poi.model_dump()
                    for field, value in incoming.items():
                        if isinstance(value, list):
                            data[field] = list(data.get(field) or [])
                            for item in value:
                                if item not in data[field]:
                                    data[field].append(item)
                        elif field in {"selection_reason", "relevance"} and value:
                            if value not in str(data.get(field, "")):
                                data[field] = "; ".join(
                                    filter(None, [data.get(field), value])
                                )
                        elif value is not None and value != "unknown":
                            data[field] = value
                    data["id"] = previous.id
                    data["is_mandatory"] = previous.is_mandatory or poi.is_mandatory
                    data["is_recommendable"] = (
                        previous.is_recommendable and poi.is_recommendable
                    )
                    if previous.business_status in {
                        "CLOSED_PERMANENTLY",
                        "CLOSED_TEMPORARILY",
                    }:
                        data["business_status"] = previous.business_status
                    if previous.date_specific_status == "closed":
                        data["date_specific_status"] = "closed"
                    if previous.priority == "must":
                        data["priority"] = "must"
                    confidence = {"unknown": 0, "low": 1, "medium": 2, "high": 3}
                    if (
                        confidence[previous.visit_duration_confidence]
                        > confidence[poi.visit_duration_confidence]
                    ):
                        for field in (
                            "visit_minutes",
                            "recommended_visit_minutes",
                            "minimum_visit_minutes",
                            "maximum_visit_minutes",
                            "visit_duration_basis",
                            "visit_duration_confidence",
                        ):
                            data[field] = getattr(previous, field)
                    resolved = {
                        "opening_hours": bool(
                            data.get("opening_hours") or data.get("opening_periods")
                        ),
                        "ticket_cost": data.get("adult_entry_cost") is not None,
                        "reservation_rules": data.get("reservation_required")
                        is not None,
                        "accessibility": ResearchTools._has_usable_accessibility_evidence(
                            POI.model_validate(data)
                        ),
                    }
                    data["unresolved_fields"] = [
                        field
                        for field in data["unresolved_fields"]
                        if not resolved.get(field, False)
                    ]
                    bucket[key] = POI.model_validate(data)
        return {stop: list(items.values()) for stop, items in result.items()}

    def shortlist_pois(
        self, trip: ResearchInput, candidates: dict[str, list[dict[str, Any]]]
    ) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        avoid = {term.strip().lower() for term in trip.avoid if term.strip()}
        major_stop_count = max(
            1,
            sum(
                any(
                    not (record.get("is_mandatory") or record.get("priority") == "must")
                    for record in records
                )
                for records in candidates.values()
            ),
        )
        required_per_stop = max(
            self.limits.min_actual_pois_per_major_base,
            math.ceil(trip.duration_days / major_stop_count * 2.5) + 1,
        )
        # One extra candidate per destination is a deterministic local backup.
        target_per_stop = min(
            self.limits.max_enriched_pois_per_stop, required_per_stop + 1
        )
        queues: dict[str, list[dict[str, Any]]] = {}
        for stop_id, records in candidates.items():
            seen, selected = set(), []
            mandatory = [
                record
                for record in records
                if record.get("is_mandatory") or record.get("priority") == "must"
            ]
            selected.extend(mandatory)
            buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for record in records:
                if record in mandatory:
                    continue
                if (
                    record.get("is_recommendable") is False
                    or record.get("business_status")
                    in {"CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"}
                    or record.get("date_specific_status") == "closed"
                ):
                    continue
                name = record["name"].lower()
                if name in seen or any(
                    term in name
                    or term in " ".join(record.get("categories", [])).lower()
                    for term in avoid
                ):
                    continue
                seen.add(name)
                category = self._category_buckets(
                    record.get("categories", []), record["name"]
                )[0]
                buckets[category].append(record)
            for bucket in buckets.values():
                bucket.sort(key=lambda item: self._candidate_rank(item, trip))
            balanced = []
            # First give each category room, then relax the diversity cap.
            for _ in range(max(0, self.limits.max_optional_pois_per_category)):
                for bucket in buckets.values():
                    if bucket:
                        balanced.append(bucket.pop(0))
            while any(buckets.values()):
                for bucket in buckets.values():
                    if bucket:
                        balanced.append(bucket.pop(0))
            attempted_here = sum(
                key[0] == stop_id for key in self.optional_enrichment_attempts
            )
            queues[stop_id] = balanced[
                : max(
                    0,
                    min(
                        target_per_stop - len(mandatory),
                        self.limits.max_enriched_pois_per_stop - attempted_here,
                    ),
                )
            ]
            result[stop_id] = selected
        remaining = max(
            0,
            self.limits.max_optional_pois_total
            - len(self.optional_enrichment_attempts),
        )
        while remaining > 0 and any(queues.values()):
            for stop_id, queue in queues.items():
                if queue and remaining > 0:
                    result[stop_id].append(queue.pop(0))
                    remaining -= 1
        return result

    async def enrich_pois(
        self, trip: ResearchInput, shortlisted: dict[str, list[dict[str, Any]]]
    ) -> dict[str, list[POI]]:
        async def one(stop_id: str, record: dict[str, Any]) -> tuple[str, POI]:
            details = await self._call(
                "google_place_details",
                self.limits.max_place_details_calls,
                f"details:{record['id']}",
                604800,
                lambda: self.provider.place_details(record),
                priority=(
                    "mandatory"
                    if record.get("is_mandatory") or record.get("priority") == "must"
                    else "optional"
                ),
            )
            evidence_id = self._evidence(
                details.get("provider", "place_details"), details["name"], details
            )
            image = (
                {
                    "provider": "google_places",
                    "place_id": details["google_place_id"],
                    "resolution_strategy": "runtime_place_photo",
                    "photo_available": None,
                }
                if details.get("google_place_id")
                else None
            )
            poi = POI(
                id=details["id"],
                google_place_id=details.get("google_place_id"),
                name=details["name"],
                stop_id=stop_id,
                coordinates=Coordinates(**details["coordinates"]),
                address=details.get("address"),
                categories=details.get("categories", []),
                normalized_categories=self._category_buckets(
                    details.get("categories", []), details["name"]
                ),
                image=image,
                priority=details.get("priority", "normal"),
                is_mandatory=details.get("is_mandatory", False),
                requirement_ids=details.get("requirement_ids", []),
                selection_reason=details.get("selection_reason"),
                relevance=details.get("relevance", "Provider candidate"),
                business_status=details.get("business_status"),
                is_recommendable=details.get("is_recommendable", True),
                visit_minutes=details.get("visit_minutes"),
                visit_duration_basis=details.get("visit_duration_basis", "unknown"),
                visit_duration_confidence=details.get(
                    "visit_duration_confidence", "unknown"
                ),
                minimum_visit_minutes=details.get("minimum_visit_minutes"),
                recommended_visit_minutes=details.get("recommended_visit_minutes"),
                maximum_visit_minutes=details.get("maximum_visit_minutes"),
                queue_or_access_minutes=details.get("queue_or_access_minutes"),
                opening_hours=details.get("opening_hours", []),
                opening_periods=details.get("opening_periods", []),
                closed_weekdays=details.get("closed_weekdays", []),
                date_specific_status=details.get("date_specific_status", "unknown"),
                special_closure_notes=details.get("special_closure_notes", []),
                last_entry_time=details.get("last_entry_time"),
                adult_entry_cost=details.get("adult_entry_cost"),
                entry_currency=details.get("entry_currency"),
                reservation_required=details.get("reservation_required"),
                booking_lead_time=details.get("booking_lead_time"),
                accessibility_notes=details.get("accessibility_notes", []),
                restrictions=details.get("restrictions", []),
                photography_restrictions=details.get("photography_restrictions", []),
                crowd_notes=details.get("crowd_notes", []),
                weather_sensitive=details.get("weather_sensitive"),
                indoor_outdoor=details.get("indoor_outdoor", "unknown"),
                evidence_ids=[evidence_id],
                unresolved_fields=details.get("unresolved_fields", []),
            )
            return stop_id, poi

        mandatory_jobs, optional_queues = [], {}
        for stop_id, records in shortlisted.items():
            optional_queues[stop_id] = []
            for record in records:
                identity = (stop_id, record.get("google_place_id") or record["id"])
                self.enrichment_attempts.add(identity)
                if record.get("is_mandatory") or record.get("priority") == "must":
                    mandatory_jobs.append((stop_id, record))
                else:
                    self.optional_enrichment_attempts.add(identity)
                    optional_queues[stop_id].append((stop_id, record))
        jobs = list(mandatory_jobs)
        while any(optional_queues.values()):
            for queue in optional_queues.values():
                if queue:
                    jobs.append(queue.pop(0))
        tasks = [one(stop_id, record) for stop_id, record in jobs]
        result: dict[str, list[POI]] = defaultdict(list)
        for outcome in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(outcome, tuple):
                stop_id, poi = outcome
                result[stop_id].append(poi)
        # Preserve a provider-discovered candidate when details are unavailable
        # or quota-capped. The planner receives a transparent low-confidence
        # fallback instead of silently losing a whole destination.
        pois = self.merge_pois(self.candidate_pois(trip, shortlisted), dict(result))
        return await self._web_enrich_pois(trip, pois)

    async def _web_enrich_pois(
        self, trip: ResearchInput, pois_by_stop: dict[str, list[POI]]
    ) -> dict[str, list[POI]]:
        """One targeted web search per selected POI, followed by one bounded LLM extraction."""
        all_pois = [
            poi
            for items in pois_by_stop.values()
            for poi in items
            if poi.is_mandatory or poi.priority == "must"
        ]
        queues = [
            [poi for poi in items if not (poi.is_mandatory or poi.priority == "must")]
            for items in pois_by_stop.values()
        ]
        while any(queues):
            for queue in queues:
                if queue:
                    all_pois.append(queue.pop(0))
        if (
            not all_pois
            or not self.reasoner
            or self.reasoner.metrics["calls"] >= self.limits.max_llm_calls
        ):
            return pois_by_stop

        async def research(poi: POI) -> tuple[str, list[dict[str, Any]]]:
            query = f"{poi.name} official visit duration adult ticket reservation accessibility stairs photography restrictions"
            results = await self._call(
                "web_search",
                self.limits.max_web_searches,
                f"poi-facts:{poi.google_place_id or poi.id}",
                21600,
                lambda: self.provider.web_search(query),
                priority="mandatory" if poi.is_mandatory else "optional",
            )
            return poi.id, results

        raw_results = {
            poi_id: results
            for outcome in await asyncio.gather(
                *(research(poi) for poi in all_pois), return_exceptions=True
            )
            if isinstance(outcome, tuple)
            for poi_id, results in [outcome]
        }
        records = []
        web_ids: dict[str, list[str]] = {}
        for poi in all_pois:
            results = raw_results.get(poi.id, [])
            web_ids[poi.id] = [
                self._evidence("tavily", item.get("title", poi.name), item, "web")
                for item in results
            ]
            records.append(
                {
                    "poi_id": poi.id,
                    "name": poi.name,
                    "accessibility_requirements": trip.travellers.accessibility_needs,
                    "web_results": [
                        {
                            "title": item.get("title"),
                            "url": item.get("url"),
                            "content": str(item.get("content", ""))[:1400],
                        }
                        for item in results
                    ],
                }
            )
        if not any(raw_results.values()):
            return pois_by_stop
        try:
            facts = await self.reasoner.interpret_poi_facts(records)
        except Exception:
            logger.warning("poi_fact_interpretation_unavailable", exc_info=True)
            facts = {}
        updated: dict[str, list[POI]] = {}
        for stop_id, items in pois_by_stop.items():
            updated[stop_id] = []
            for poi in items:
                fact = facts.get(poi.id, {}) if raw_results.get(poi.id) else {}
                supported = {
                    key: value
                    for key, value in fact.items()
                    if key
                    in {
                        "visit_minutes",
                        "minimum_visit_minutes",
                        "recommended_visit_minutes",
                        "maximum_visit_minutes",
                        "queue_or_access_minutes",
                        "adult_entry_cost",
                        "entry_currency",
                        "reservation_required",
                        "booking_lead_time",
                        "accessibility_notes",
                        "restrictions",
                        "photography_restrictions",
                        "crowd_notes",
                        "weather_sensitive",
                        "special_closure_notes",
                        "last_entry_time",
                        "unresolved_fields",
                    }
                }
                if (
                    supported.get("visit_minutes") is not None
                    or supported.get("recommended_visit_minutes") is not None
                ):
                    supported["visit_duration_basis"] = "source_estimate"
                    supported["visit_duration_confidence"] = "medium"
                    supported.setdefault(
                        "recommended_visit_minutes", supported.get("visit_minutes")
                    )
                supported["evidence_ids"] = [*poi.evidence_ids, *web_ids[poi.id]]
                if (
                    supported.get("recommended_visit_minutes")
                    and poi.visit_duration_basis == "estimated_from_poi_characteristics"
                ):
                    duration = supported["recommended_visit_minutes"]
                    supported.setdefault(
                        "minimum_visit_minutes",
                        min(poi.minimum_visit_minutes or duration, duration),
                    )
                    supported.setdefault(
                        "maximum_visit_minutes",
                        max(poi.maximum_visit_minutes or duration, duration),
                    )
                # Revalidate LLM-interpreted values at the Pydantic boundary.
                try:
                    enriched = POI.model_validate({**poi.model_dump(), **supported})
                except (ValueError, TypeError):
                    logger.warning(
                        "invalid_poi_fact_interpretation", extra={"poi_id": poi.id}
                    )
                    enriched = poi
                merged = self.merge_pois({stop_id: [poi]}, {stop_id: [enriched]})[
                    stop_id
                ][0]
                updated[stop_id].append(self._duration_fallback(trip, merged))
        return updated

    @staticmethod
    def _duration_fallback(trip: ResearchInput, poi: POI) -> POI:
        """Planner-safe estimate only when research could not establish a duration."""
        if poi.recommended_visit_minutes:
            return poi
        if poi.visit_minutes:
            recommended = poi.visit_minutes
            return poi.model_copy(
                update={
                    "minimum_visit_minutes": poi.minimum_visit_minutes
                    or max(15, recommended - 15),
                    "recommended_visit_minutes": recommended,
                    "maximum_visit_minutes": poi.maximum_visit_minutes
                    or recommended + 30,
                    "visit_duration_basis": (
                        poi.visit_duration_basis
                        if poi.visit_duration_basis != "unknown"
                        else "source_estimate"
                    ),
                    "visit_duration_confidence": (
                        poi.visit_duration_confidence
                        if poi.visit_duration_confidence != "unknown"
                        else "medium"
                    ),
                }
            )
        walking_sensitive = bool(trip.travellers.accessibility_needs)
        minimum, recommended, maximum = (30, 45, 60)
        if poi.priority == "must" and walking_sensitive:
            minimum, recommended, maximum = (45, 60, 90)
        unresolved = [
            item for item in poi.unresolved_fields if item != "visit_duration"
        ]
        return poi.model_copy(
            update={
                "visit_minutes": recommended,
                "minimum_visit_minutes": minimum,
                "recommended_visit_minutes": recommended,
                "maximum_visit_minutes": maximum,
                "queue_or_access_minutes": 15 if walking_sensitive else 0,
                "visit_duration_basis": "estimated_from_poi_characteristics",
                "visit_duration_confidence": "low",
                "unresolved_fields": unresolved,
            }
        )

    def cluster_pois(
        self, pois_by_stop: dict[str, list[POI]]
    ) -> list[GeographicCluster]:
        clusters = []
        for stop_id, pois in pois_by_stop.items():
            groups: list[list[POI]] = []
            for poi in pois:
                # A simple, explainable 5 km greedy cluster keeps distant city POIs
                # from being presented as one convenient day area.
                matching = next(
                    (
                        group
                        for group in groups
                        if _haversine(poi.coordinates, group[0].coordinates) <= 5
                    ),
                    None,
                )
                (
                    matching
                    if matching is not None
                    else groups.append([]) or groups[-1]
                ).append(poi)
            for index, group in enumerate(groups, start=1):
                lat = sum(item.coordinates.latitude for item in group) / len(group)
                lon = sum(item.coordinates.longitude for item in group) / len(group)
                clusters.append(
                    GeographicCluster(
                        id=f"cluster_{stop_id}_{index}",
                        stop_id=stop_id,
                        name=f"{stop_id} area {index}",
                        poi_ids=[item.id for item in group],
                        centroid=Coordinates(latitude=lat, longitude=lon),
                        rationale="POIs within approximately 5 km of the first POI in this area.",
                    )
                )
        return clusters

    @staticmethod
    def _expected_activity_minutes(trip: ResearchInput) -> int:
        """Conservative minimum activity capacity; travel and meals remain planner time."""
        # The first or last day commonly includes arrival/departure, so it has a
        # smaller activity allowance. This is deliberately an MVP floor, not a
        # claim that every minute of a holiday should be scheduled.
        return max(90, trip.duration_days * 90 - (90 if trip.duration_days > 1 else 0))

    @staticmethod
    def _normalized_poi_name(poi: POI) -> str:
        return re.sub(r"\W+", "", poi.name.casefold())

    @staticmethod
    def _coordinate_key(poi: POI) -> tuple[float, float]:
        # Four decimals is roughly eleven metres.  Two records at this distance
        # are not independent itinerary choices.
        return (round(poi.coordinates.latitude, 4), round(poi.coordinates.longitude, 4))

    @staticmethod
    def _has_hard_accessibility_requirement(trip: ResearchInput) -> bool:
        return any(
            any(
                marker in need.casefold()
                for marker in (
                    "hard",
                    "must",
                    "required",
                    "mandatory",
                    "wheelchair",
                    "step-free",
                    "step free",
                )
            )
            for need in trip.travellers.accessibility_needs
        )

    @staticmethod
    def _has_usable_accessibility_evidence(poi: POI) -> bool:
        """Accept explicit evidence, but never turn an unknown/negative note into access."""
        text = " ".join(poi.accessibility_notes).casefold().strip()
        if not text:
            return False
        unresolved = (
            "unknown",
            "not available",
            "unavailable",
            "no information",
            "not verified",
            "confirm accessibility",
        )
        negative = (
            "not accessible",
            "no wheelchair",
            "wheelchair unavailable",
            "stairs only",
            "not step free",
            "not step-free",
            "partially accessible",
            "partial accessibility",
            "limited accessibility",
            "not wheelchair",
            "inaccessible",
            "accessible: false",
            "accessible: no",
            "access: false",
            "access: no",
            "requires assistance",
            "some areas only",
        )
        positive = (
            "wheelchair accessible",
            "wheelchair-accessible",
            "step-free",
            "step free",
            "accessible entrance",
            "wheelchair access",
        )
        return any(term in text for term in positive) and not any(
            term in text for term in (*unresolved, *negative)
        )

    @staticmethod
    def _has_schedulable_opening_opportunity(poi: POI, trip: ResearchInput) -> bool:
        """Use the planner's own opening-period logic to prove one viable visit slot."""
        duration = poi.recommended_visit_minutes or poi.visit_minutes
        if duration is None or duration <= 0:
            return False
        current = (
            trip.start_date
            if isinstance(trip.start_date, date)
            else date.fromisoformat(str(trip.start_date))
        )
        end_date = (
            trip.end_date
            if isinstance(trip.end_date, date)
            else date.fromisoformat(str(trip.end_date))
        )
        while current <= end_date:
            if not poi.opening_periods and current.strftime("%A").casefold() not in {
                day.casefold() for day in poi.closed_weekdays
            }:
                return True
            bounds = opening_bounds(poi, current.isoformat())
            if any(close - opens >= duration for opens, close in bounds):
                return True
            current += timedelta(days=1)
        return False

    def _usable_distinct_pois(
        self,
        items: list[POI],
        trip: ResearchInput,
        *,
        require_accessibility: bool = False,
    ) -> list[POI]:
        """Count usable research, including explicitly estimated visit durations."""
        seen_ids = set()
        usable: list[POI] = []
        for poi in items:
            identity = poi.google_place_id or poi.id
            if (
                not poi.is_recommendable
                or poi.business_status in {"CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"}
                or not poi.recommended_visit_minutes
                or poi.recommended_visit_minutes <= 0
                or not identity
                or not poi.name.strip()
                or poi.date_specific_status == "closed"
                or (
                    (poi.opening_periods or poi.closed_weekdays)
                    and not self._has_schedulable_opening_opportunity(poi, trip)
                )
                or (
                    require_accessibility
                    and not self._has_usable_accessibility_evidence(poi)
                )
            ):
                continue
            if identity in seen_ids or any(
                not (poi.google_place_id and other.google_place_id)
                and self._normalized_poi_name(poi) == self._normalized_poi_name(other)
                and _haversine(poi.coordinates, other.coordinates) <= 0.05
                for other in usable
            ):
                continue
            seen_ids.add(identity)
            usable.append(poi)
        return usable

    def _destination_activity_shares(
        self, trip: ResearchInput, bases: list[ResolvedStop]
    ) -> tuple[list[int], str]:
        """Allocate provisional capacity without pretending equal nights are known."""
        expected = self._expected_activity_minutes(trip)
        requested = trip.destination_context.get("destination_days")
        if isinstance(requested, dict):
            values = [
                max(0, int(requested.get(stop.id, requested.get(stop.name, 0))))
                for stop in bases
            ]
            if sum(values) > 0:
                total = sum(values)
                raw = [expected * value / total for value in values]
                allocated = [math.floor(value) for value in raw]
                for index in sorted(
                    range(len(raw)),
                    key=lambda item: raw[item] - allocated[item],
                    reverse=True,
                )[: expected - sum(allocated)]:
                    allocated[index] += 1
                return allocated, "destination_days"
        # The collection input does not yet contain nights per destination.  Give
        # every requested base one minimum share, then assign remaining capacity
        # to the first explicitly requested destinations in route order.
        if len(bases) == 1:
            return [expected], "single_destination"
        minimum = min(90, expected // len(bases))
        remaining = max(0, expected - minimum * len(bases))
        weights = list(range(len(bases), 0, -1))
        weight_total = sum(weights)
        allocated = [
            minimum + math.floor(remaining * weight / weight_total)
            for weight in weights
        ]
        for index in range(expected - sum(allocated)):
            allocated[index % len(allocated)] += 1
        return allocated, "provisional_destination_order"

    @staticmethod
    def _coverage_stops(
        trip: ResearchInput, stops: list[ResolvedStop]
    ) -> list[ResolvedStop]:
        if trip.destination.type == "route":
            base_stops = [
                stop for stop in stops if ResearchTools._is_base_like_stop(stop)
            ]
            if base_stops:
                return base_stops
            return [
                stop
                for stop in stops
                if stop.type in {"circuit_stop", "mandatory_poi", "mandatory_base"}
            ] or stops
        return [
            stop
            for stop in stops
            if stop.type not in {"mandatory_poi", "circuit_stop", "endpoint"}
        ] or stops

    def measure_coverage(
        self,
        trip: ResearchInput,
        stops: list[ResolvedStop],
        pois_by_stop: dict[str, list[POI]],
        routes: tuple[list[RouteEvidence], list[RouteEvidence]],
        transport_requirements: list[TransportRequirement] | None = None,
    ) -> ResearchCoverage:
        """Measure whether factual activity coverage can support the trip length."""
        bases = self._coverage_stops(trip, stops)
        route_is_circuit = trip.destination.type == "route" and not any(
            self._is_base_like_stop(stop) for stop in stops
        )
        expected = self._expected_activity_minutes(trip)
        allocations, allocation_basis = self._destination_activity_shares(trip, bases)
        require_accessibility = self._has_hard_accessibility_requirement(trip)
        rows: list[DestinationCoverage] = []
        for index, stop in enumerate(bases):
            required = allocations[index]
            poi_items = self._usable_distinct_pois(
                pois_by_stop.get(stop.id, []),
                trip,
                require_accessibility=require_accessibility,
            )
            researched = sum(
                poi.recommended_visit_minutes or poi.visit_minutes or 45
                for poi in poi_items
            )
            rows.append(
                DestinationCoverage(
                    stop_id=stop.id,
                    stop_name=stop.name,
                    required_activity_minutes=required,
                    researched_activity_minutes=researched,
                    primary_poi_count=max(0, len(poi_items) - 1),
                    backup_poi_count=1 if len(poi_items) > 1 else 0,
                    distinct_poi_count=len(poi_items),
                    schedulable_poi_count=len(poi_items),
                    allocation_basis=allocation_basis,
                    sufficient=(
                        researched >= required
                        # A circuit member is itself a required visit, unlike a
                        # city base which needs several alternative activities.
                        # Exact-member validation below remains the route gate.
                        and len(poi_items)
                        >= (
                            1
                            if route_is_circuit
                            else self.limits.min_actual_pois_per_major_base
                        )
                    ),
                )
            )
        all_pois = [poi for items in pois_by_stop.values() for poi in items]
        usable_pois = [
            poi
            for values in pois_by_stop.values()
            for poi in self._usable_distinct_pois(
                values, trip, require_accessibility=require_accessibility
            )
        ]
        # The graph constructs this set before it performs routing.  Never
        # infer completeness from routes that happened to be returned: that
        # was the source of the hidden missing-entry/exit-leg bug.
        requirements = transport_requirements or []
        required_transport = len(requirements)
        represented = sum(item.status != "missing" for item in requirements)
        verified = sum(item.status == "verified" for item in requirements)
        usable_transport = sum(
            item.status in {"verified", "unsupported_by_provider"}
            for item in requirements
        )
        transport_coverage = (
            usable_transport / required_transport if required_transport else 0.0
        )
        requirements_for_trip = self._requirements_for(trip)
        required_must_sees = [item for item in requirements_for_trip if item.required]
        covered_must_sees = 0
        for requirement in required_must_sees:
            if requirement.requirement_type == "destination_or_base":
                covered = any(
                    requirement.id in self.requirement_stop_ids
                    and stop.id in self.requirement_stop_ids[requirement.id]
                    for stop in stops
                )
            elif requirement.requirement_type == "all_route_members":
                route_members = [
                    stop for stop in stops if not self._is_base_like_stop(stop)
                ]
                covered = bool(route_members) and all(
                    any(poi.stop_id == stop.id and poi.is_mandatory for poi in all_pois)
                    for stop in route_members
                )
            else:
                covered = any(requirement.id in poi.requirement_ids for poi in all_pois)
            covered_must_sees += int(covered)
        must_see_coverage = (
            covered_must_sees / len(required_must_sees) if required_must_sees else 1.0
        )
        return ResearchCoverage(
            trip_days=trip.duration_days,
            expected_activity_minutes=expected,
            researched_activity_minutes=sum(
                poi.recommended_visit_minutes or poi.visit_minutes or 0
                for poi in usable_pois
            ),
            destination_coverage=rows,
            must_see_coverage=round(must_see_coverage, 3),
            backup_poi_count=sum(item.backup_poi_count for item in rows),
            transport_coverage=round(transport_coverage, 3),
            required_transport_leg_count=required_transport,
            represented_transport_leg_count=represented,
            verified_transport_leg_count=verified,
            sufficient_for_planning=(
                bool(rows)
                and all(item.sufficient for item in rows)
                and must_see_coverage == 1.0
                and required_transport > 0
                and transport_coverage == 1.0
            ),
        )

    @staticmethod
    def build_stay_bases(
        stops: list[ResolvedStop], clusters: list[GeographicCluster]
    ) -> list[StayBase]:
        """Expose practical areas, not live hotel inventory or booking claims."""
        cluster_ids: dict[str, list[str]] = defaultdict(list)
        for cluster in clusters:
            cluster_ids[cluster.stop_id].append(cluster.id)
        bases = [
            StayBase(
                stop_id=stop.id,
                name=f"{stop.name} central/base area",
                coordinates=stop.coordinates,
                rationale=(
                    "Resolved destination area, not a booked hotel; confirm accommodation and access separately."
                ),
                nearby_cluster_ids=cluster_ids.get(stop.id, []),
            )
            for stop in stops
            if stop.type not in {"mandatory_poi", "circuit_stop", "endpoint"}
        ]
        if not bases and stops:
            stop = stops[0]
            bases.append(
                StayBase(
                    stop_id=stop.id,
                    name=f"{stop.name} provisional base area",
                    coordinates=stop.coordinates,
                    rationale="Starting area for the resolved circuit; accommodation suitability and overnight stops still require planning.",
                    nearby_cluster_ids=cluster_ids.get(stop.id, []),
                )
            )
        for base in list(bases):
            nearby = sorted(
                (cluster for cluster in clusters if cluster.stop_id == base.stop_id),
                key=lambda cluster: (-len(cluster.poi_ids), cluster.id),
            )
            for cluster in nearby[:2]:
                if _haversine(base.coordinates, cluster.centroid) < 1:
                    continue
                bases.append(
                    StayBase(
                        stop_id=base.stop_id,
                        name=f"{cluster.name} stay-area alternative",
                        coordinates=cluster.centroid,
                        rationale="Area near a group of researched activities, not verified hotel inventory or an accessibility guarantee.",
                        nearby_cluster_ids=[cluster.id],
                    )
                )
        return bases

    def research_gaps(
        self,
        coverage: ResearchCoverage,
        quality: ResearchQuality,
        transport_requirements: list[TransportRequirement] | None = None,
        pois_by_stop: dict[str, list[POI]] | None = None,
    ) -> list[ResearchGap]:
        gaps: list[ResearchGap] = []
        thin = [item for item in coverage.destination_coverage if not item.sufficient]
        if thin:
            gaps.append(
                ResearchGap(
                    code="destination_activity_coverage",
                    severity="critical",
                    message="Insufficient researched activity capacity for: "
                    + ", ".join(item.stop_name for item in thin),
                    stop_ids=[item.stop_id for item in thin],
                    recommended_action="Discover and enrich additional POIs only for these destinations.",
                )
            )
        unresolved_transport = [
            item
            for item in transport_requirements or []
            if item.status in {"missing", "unavailable"}
        ]
        if unresolved_transport:
            gaps.append(
                ResearchGap(
                    code="major_transport_evidence",
                    severity="critical",
                    message="Major arrival, departure, or inter-destination transport evidence is incomplete.",
                    recommended_action="Research only the unresolved major transport legs and retain explicit unavailable facts if no source can verify them.",
                )
            )
        optional_closed = [
            poi.stop_id
            for values in (pois_by_stop or {}).values()
            for poi in values
            if poi.business_status in {"CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"}
            and not (poi.is_mandatory or poi.priority == "must" or poi.requirement_ids)
        ]
        if optional_closed:
            gaps.append(
                ResearchGap(
                    code="optional_closed_candidates",
                    severity="optional",
                    message="Optional closed POIs are excluded and should be replaced only if destination coverage is thin.",
                    stop_ids=sorted(set(optional_closed)),
                    recommended_action="Retain usable candidates and discover replacement POIs only for affected destinations.",
                )
            )
        for message in quality.critical_gaps:
            if "Destination could not be resolved" in message:
                gaps.append(
                    ResearchGap(
                        code="destination_resolution",
                        severity="critical",
                        message=message,
                        recommended_action="Retry destination resolution with a targeted query.",
                    )
                )
        for message in quality.optional_gaps:
            gaps.append(
                ResearchGap(
                    code="research_detail_gap",
                    severity="optional",
                    message=message,
                    recommended_action="Retain this as a visible planning assumption or verify it later.",
                )
            )
        return gaps

    async def research_routes(
        self,
        trip: ResearchInput,
        stops: list[ResolvedStop],
        clusters: list[GeographicCluster],
        *,
        repair: bool = False,
        repair_requirement_ids: set[str] | None = None,
        previous_requirements: list[TransportRequirement] | None = None,
        previous_routes: tuple[list[RouteEvidence], list[RouteEvidence]] | None = None,
    ) -> tuple[list[RouteEvidence], list[RouteEvidence], list[TransportRequirement]]:
        """Research a deterministic, bounded set of major and local movement legs."""
        if not stops:
            return [], [], []

        def select_mode(values: list[str], *, leg_kind: str, fallback: str) -> str:
            preferred = {item.casefold().strip() for item in values}
            aliases = {
                "flight": {"flight", "fly", "plane", "air"},
                "transit": {
                    "transit",
                    "public_transport",
                    "public transit",
                    "bus",
                    "train",
                    "metro",
                },
                "driving": {
                    "private_car",
                    "rental_car",
                    "taxi",
                    "road",
                    "driving",
                    "car",
                },
                "walking": {"walk", "walking"},
                "cycling": {"cycle", "cycling", "bicycle"},
            }
            requested = {mode for mode, names in aliases.items() if preferred & names}
            if leg_kind in {"entry", "exit"}:
                order = ("flight", "transit", "driving", "walking", "cycling")
            elif leg_kind == "intercity":
                order = ("transit", "flight", "driving", "walking", "cycling")
            else:
                order = ("walking", "transit", "driving", "cycling", "flight")
            return next((mode for mode in order if mode in requested), fallback)

        entry_mode = select_mode(
            trip.transport_preferences.long_distance_modes,
            leg_kind="entry",
            fallback="driving",
        )
        intercity_mode = select_mode(
            trip.transport_preferences.long_distance_modes,
            leg_kind="intercity",
            fallback="driving",
        )
        exit_mode = select_mode(
            trip.transport_preferences.long_distance_modes,
            leg_kind="exit",
            fallback="driving",
        )
        local_mode = select_mode(
            trip.transport_preferences.local_modes,
            leg_kind="local_access",
            fallback="walking",
        )
        supports = getattr(
            self.provider,
            "supports_route",
            lambda mode: mode in {"driving", "walking", "cycling"},
        )
        targeted_ids = set(repair_requirement_ids or ())
        targeted_repair = bool(targeted_ids)
        prior_by_id = {item.id: item for item in previous_requirements or []}

        async def resolve_endpoint(
            location: str, *, retry: bool
        ) -> ResolvedStop | None:
            try:
                records = await self._call(
                    "google_text_search",
                    self.limits.max_google_searches,
                    f"{'repair:' if retry else ''}endpoint:{location}",
                    86400,
                    lambda: self.provider.resolve_destination("city", location, None),
                )
            except Exception:
                logger.warning("endpoint_resolution_unavailable", exc_info=True)
                records = []
            if not records:
                return None
            endpoint = records[0]
            evidence_id = self._evidence(
                endpoint.get("provider", "endpoint_provider"),
                endpoint["name"],
                endpoint,
            )
            return ResolvedStop(
                id=endpoint["id"],
                google_place_id=endpoint.get("google_place_id"),
                name=endpoint["name"],
                type="endpoint",
                coordinates=Coordinates(**endpoint["coordinates"]),
                address=endpoint.get("address"),
                rationale="Trip endpoint resolved for route evidence.",
                confidence="high",
                evidence_ids=[evidence_id],
            )

        resolve_entry = not targeted_repair or "entry" in targeted_ids
        resolve_exit = not targeted_repair or "exit" in targeted_ids
        start_endpoint = (
            await resolve_endpoint(trip.origin, retry=repair and resolve_entry)
            if resolve_entry
            else None
        )
        requested_end = trip.end_location or trip.origin
        if requested_end.casefold().strip() == trip.origin.casefold().strip():
            if resolve_exit:
                end_endpoint = start_endpoint or await resolve_endpoint(
                    trip.origin, retry=repair
                )
            else:
                end_endpoint = None
        else:
            end_endpoint = (
                await resolve_endpoint(requested_end, retry=repair and resolve_exit)
                if resolve_exit
                else None
            )

        jobs: list[tuple[TransportRequirement, ResolvedStop, ResolvedStop, bool]] = []

        def required(
            identifier: str,
            kind: str,
            origin: ResolvedStop | None,
            destination: ResolvedStop | None,
            origin_label: str,
            destination_label: str,
            mode: str,
            local: bool = False,
        ) -> None:
            previous = prior_by_id.get(identifier)
            if targeted_repair and identifier not in targeted_ids:
                if previous is not None:
                    requirements.append(previous)
                    return
                # A malformed prior state must remain visibly incomplete rather
                # than expanding a targeted repair into unrelated provider calls.
                requirements.append(
                    TransportRequirement(
                        id=identifier,
                        leg_kind=kind,
                        origin_id=origin.id if origin else None,
                        destination_id=destination.id if destination else None,
                        origin_label=origin_label,
                        destination_label=destination_label,
                        preferred_mode=mode,
                        status="missing",
                    )
                )
                return
            requirement = TransportRequirement(
                id=identifier,
                leg_kind=kind,
                origin_id=origin.id if origin else None,
                destination_id=destination.id if destination else None,
                origin_label=origin_label,
                destination_label=destination_label,
                preferred_mode=mode,
                status="missing" if not origin or not destination else "unavailable",
            )
            requirements.append(requirement)
            if origin and destination:
                jobs.append((requirement, origin, destination, local))

        requirements: list[TransportRequirement] = []
        required(
            "entry",
            "entry",
            start_endpoint,
            stops[0],
            trip.origin,
            stops[0].name,
            entry_mode,
        )
        for index, (origin, destination) in enumerate(pairwise(stops), 1):
            required(
                f"intercity:{index}",
                "intercity",
                origin,
                destination,
                origin.name,
                destination.name,
                intercity_mode,
            )
        required(
            "exit",
            "exit",
            stops[-1],
            end_endpoint,
            stops[-1].name,
            requested_end,
            exit_mode,
        )

        by_stop: dict[str, list[GeographicCluster]] = defaultdict(list)
        for cluster in clusters:
            by_stop[cluster.stop_id].append(cluster)
        for stop_id, stop_clusters in by_stop.items():
            # One representative cluster-to-cluster leg per destination proves
            # local movement is considered without building a paid all-POI matrix.
            if len(stop_clusters) < 2:
                continue
            before, after = stop_clusters[0], stop_clusters[1]
            origin_evidence = self._evidence(
                "derived_cluster",
                before.name,
                {"cluster_id": before.id, "centroid": before.centroid.model_dump()},
            )
            destination_evidence = self._evidence(
                "derived_cluster",
                after.name,
                {"cluster_id": after.id, "centroid": after.centroid.model_dump()},
            )
            origin = ResolvedStop(
                id=before.id,
                name=before.name,
                type="cluster",
                coordinates=before.centroid,
                rationale="Cluster centroid",
                confidence="medium",
                evidence_ids=[origin_evidence],
            )
            destination = ResolvedStop(
                id=after.id,
                name=after.name,
                type="cluster",
                coordinates=after.centroid,
                rationale="Cluster centroid",
                confidence="medium",
                evidence_ids=[destination_evidence],
            )
            required(
                f"local:{stop_id}",
                "local_access",
                origin,
                destination,
                before.name,
                after.name,
                local_mode,
                True,
            )

        async def route_job(
            requirement: TransportRequirement,
            origin: ResolvedStop,
            destination: ResolvedStop,
            local: bool,
        ) -> tuple[TransportRequirement, RouteEvidence | None, bool]:
            mode = requirement.preferred_mode
            straight = _haversine(origin.coordinates, destination.coordinates)
            if not supports(mode):
                route = RouteEvidence(
                    origin_id=origin.id,
                    destination_id=destination.id,
                    mode=mode,
                    relevant_modes=trip.transport_preferences.local_modes
                    if local
                    else trip.transport_preferences.long_distance_modes,
                    preferred_modes=[mode],
                    straight_line_km=straight,
                    status="unsupported_by_provider",
                )
                return (
                    requirement.model_copy(
                        update={"status": "unsupported_by_provider"}
                    ),
                    route,
                    local,
                )
            try:
                raw = await self._call(
                    "routes",
                    self.limits.max_route_calls,
                    f"route:{'repair:' if targeted_repair else ''}{origin.id}:{destination.id}:{mode}",
                    86400,
                    lambda: self.provider.route(
                        origin.model_dump(), destination.model_dump(), mode
                    ),
                )
            except Exception:
                logger.warning("research_route_unavailable", exc_info=True)
                raw = None
            if not raw:
                route = RouteEvidence(
                    origin_id=origin.id,
                    destination_id=destination.id,
                    mode=mode,
                    relevant_modes=trip.transport_preferences.local_modes
                    if local
                    else trip.transport_preferences.long_distance_modes,
                    preferred_modes=[mode],
                    straight_line_km=straight,
                    status="unavailable",
                )
                return requirement, route, local
            evidence_id = self._evidence(
                raw.get("provider", "routes"),
                f"{origin.name} to {destination.name}",
                raw,
            )
            route = RouteEvidence(
                origin_id=origin.id,
                destination_id=destination.id,
                mode=raw.get("mode", mode),
                relevant_modes=trip.transport_preferences.local_modes
                if local
                else trip.transport_preferences.long_distance_modes,
                preferred_modes=[mode],
                distance_km=raw.get("distance_km"),
                duration_minutes=raw.get("duration_minutes"),
                straight_line_km=straight,
                status="verified",
                evidence_ids=[evidence_id],
            )
            return (
                requirement.model_copy(
                    update={"status": "verified", "route_evidence_ids": [evidence_id]}
                ),
                route,
                local,
            )

        previous_inter, previous_intra = previous_routes or ([], [])
        targeted_pairs = {
            (item.origin_id, item.destination_id)
            for item in prior_by_id.values()
            if item.id in targeted_ids
        }
        inter = [
            route
            for route in previous_inter
            if (route.origin_id, route.destination_id) not in targeted_pairs
        ]
        intra = [
            route
            for route in previous_intra
            if (route.origin_id, route.destination_id) not in targeted_pairs
        ]
        outcomes = await asyncio.gather(
            *(route_job(*job) for job in jobs), return_exceptions=True
        )
        by_requirement = {item.id: item for item in requirements}
        for outcome in outcomes:
            if not isinstance(outcome, tuple):
                continue
            requirement, route, local = outcome
            by_requirement[requirement.id] = requirement
            if route:
                (intra if local else inter).append(route)

        def distinct_routes(items):
            merged = {}
            for route in items:
                key = (route.origin_id, route.destination_id, route.mode)
                previous = merged.get(key)
                if (
                    previous is None
                    or route.status == "verified"
                    or previous.status != "verified"
                ):
                    merged[key] = route
            return list(merged.values())

        return (
            distinct_routes(inter),
            distinct_routes(intra),
            [by_requirement[item.id] for item in requirements],
        )

    def validate_quality(
        self,
        trip: ResearchInput,
        stops: list[ResolvedStop],
        pois: dict[str, list[POI]],
        clusters: list[GeographicCluster],
        routes: tuple[list[RouteEvidence], list[RouteEvidence]],
        coverage: ResearchCoverage | None = None,
        transport_requirements: list[TransportRequirement] | None = None,
    ) -> ResearchQuality:
        critical, optional = [], []
        all_pois = [poi for values in pois.values() for poi in values]
        if not stops:
            critical.append("Destination could not be resolved with provider evidence.")
        if stops and not all_pois:
            critical.append(
                "No usable POIs were researched for the resolved destination."
            )
        coverage = coverage or self.measure_coverage(
            trip, stops, pois, routes, transport_requirements
        )
        thin_bases = [
            item.stop_name
            for item in coverage.destination_coverage
            if not item.sufficient
        ]
        if thin_bases:
            critical.append(
                "Insufficient duration-aware activity coverage for: "
                + ", ".join(thin_bases)
            )
        if self._has_hard_accessibility_requirement(trip) and thin_bases:
            critical.append(
                "Accessibility evidence is insufficient to support the required "
                "activity capacity for: " + ", ".join(thin_bases)
            )
        requirements = transport_requirements or []
        missing_transport = [
            item for item in requirements if item.status in {"missing", "unavailable"}
        ]
        if missing_transport:
            critical.append(
                "Required transport evidence is missing or unavailable for: "
                + ", ".join(item.id for item in missing_transport)
            )
        route_members = (
            [stop for stop in stops if not self._is_base_like_stop(stop)]
            if trip.destination.type == "route"
            else []
        )
        route_is_circuit = bool(route_members) and not any(
            self._is_base_like_stop(stop) for stop in stops
        )
        if route_members:
            # City bases are covered by their own activity pool; only visitable
            # route members must survive as exact mandatory POIs.
            uncovered = [
                stop.name
                for stop in route_members
                if not any(
                    poi.stop_id == stop.id
                    and poi.priority == "must"
                    and poi.google_place_id
                    and poi.google_place_id == stop.google_place_id
                    for poi in all_pois
                )
            ]
            if uncovered:
                critical.append(
                    f"Route/circuit members are missing exact mandatory POIs: {', '.join(uncovered)}"
                )
        expected_count = self._required_member_count(trip) if route_is_circuit else 0
        resolved_member_count = sum(
            any(
                poi.stop_id == stop.id
                and poi.priority == "must"
                and poi.google_place_id
                and poi.google_place_id == stop.google_place_id
                for poi in all_pois
            )
            for stop in route_members
        )
        if (
            expected_count
            and resolved_member_count < expected_count
        ):
            critical.append(
                f"Only {resolved_member_count} of {expected_count} explicitly required route members were resolved."
            )
        unresolved = []
        for requirement in self._requirements_for(trip):
            if requirement.requirement_type == "all_route_members":
                continue
            if requirement.requirement_type == "destination_or_base":
                covered = any(
                    requirement.id in ids
                    or requirement.requirement_text.casefold() in stop.name.casefold()
                    for ids, stop in [
                        (self.requirement_stop_ids.get(requirement.id, set()), stop)
                        for stop in stops
                    ]
                )
            else:
                covered = any(requirement.id in poi.requirement_ids for poi in all_pois)
            if not covered:
                unresolved.append(requirement.requirement_text)
        if unresolved:
            critical.append(
                f"Must-see requirements unresolved: {', '.join(unresolved)}"
            )
        unsupported_transport = [
            item for item in requirements if item.status == "unsupported_by_provider"
        ]
        if unsupported_transport:
            optional.append(
                "Some required transport modes are unsupported by the current provider and remain explicit planner follow-ups."
            )
        if any(poi.visit_minutes is None for poi in all_pois):
            optional.append("Some POI visit durations remain unresolved.")
        required_poi_ids = {
            requirement.id
            for requirement in self._requirements_for(trip)
            if requirement.required
            and requirement.requirement_type != "destination_or_base"
        }

        def is_mandatory_poi(poi: POI) -> bool:
            return (
                poi.is_mandatory
                or poi.priority == "must"
                or bool(set(poi.requirement_ids) & required_poi_ids)
            )

        mandatory_closed = [
            poi.name
            for poi in all_pois
            if (
                poi.business_status in {"CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"}
                or poi.date_specific_status == "closed"
            )
            and is_mandatory_poi(poi)
        ]
        optional_closed = [
            poi.name
            for poi in all_pois
            if poi.business_status in {"CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"}
            and not is_mandatory_poi(poi)
        ]
        if mandatory_closed:
            critical.append(
                f"Mandatory POIs are closed or require replacement: {', '.join(mandatory_closed)}"
            )
        if self._has_hard_accessibility_requirement(trip):
            inaccessible_mandatory = [
                poi.name
                for poi in all_pois
                if is_mandatory_poi(poi)
                and not self._has_usable_accessibility_evidence(poi)
            ]
            if inaccessible_mandatory:
                optional.append(
                    "Mandatory POI accessibility is unverified or conflicting: "
                    + ", ".join(inaccessible_mandatory)
                )
        if optional_closed:
            optional.append(
                "Optional closed POIs were excluded from usable research coverage."
            )
        if any(not poi.opening_hours for poi in all_pois):
            optional.append("Some POI opening hours remain unresolved.")
        if (
            trip.travellers.accessibility_needs
            and not self._has_hard_accessibility_requirement(trip)
            and any(
                not poi.accessibility_notes for poi in all_pois if is_mandatory_poi(poi)
            )
        ):
            message = (
                "Accessibility notes remain unresolved for one or more mandatory POIs."
            )
            optional.append(message)
        if all_pois and not clusters:
            critical.append("Geographic clusters were not produced.")
        # Handoff accepts useful partial research; gaps remain visible rather
        # than silently claiming transport, accessibility or timing is verified.
        structural = []
        if not stops:
            structural.append(
                "Destination could not be resolved with provider evidence."
            )
        if stops and not any(
            self._usable_distinct_pois(items, trip) for items in pois.values()
        ):
            structural.append(
                "No usable POIs were researched for the resolved destination."
            )
        optional = list(
            dict.fromkeys(optional + [gap for gap in critical if gap not in structural])
        )
        critical = structural
        if any(poi.visit_duration_confidence == "low" for poi in all_pois):
            optional.append(
                "Some visit durations are planning estimates, not provider-verified facts."
            )
        if any(poi.unresolved_fields for poi in all_pois):
            optional.append(
                "Some candidate facts remain unknown; verify before booking or visiting."
            )
        sufficient = not critical
        score = max(0.0, 1.0 - 0.25 * len(critical) - 0.05 * len(optional))
        return ResearchQuality(
            completeness_score=score,
            critical_gaps=critical,
            optional_gaps=optional,
            sufficient_for_planning=sufficient,
        )


def _haversine(a: Coordinates, b: Coordinates) -> float:
    radius = 6371.0
    lat1, lat2 = math.radians(a.latitude), math.radians(b.latitude)
    d_lat, d_lon = lat2 - lat1, math.radians(b.longitude - a.longitude)
    value = (
        math.sin(d_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    )
    return round(2 * radius * math.asin(math.sqrt(value)), 2)
