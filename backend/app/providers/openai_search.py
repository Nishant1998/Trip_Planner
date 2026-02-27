"""One source-checked web research call for current trip-planning ranges."""

from __future__ import annotations

import json
from asyncio import sleep
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any, Literal, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel, Field

from app.models.evidence import (
    CostBaselineEvidence,
    CurrentTravelResearch,
    FactStatus,
    IntercityOptionEvidence,
    PlaceEvidence,
    SourceRef,
    VisitDurationEvidence,
    WebFactEvidence,
)
from app.models.context import PlanningContext, ResolvedDestination


class _CitedFact(BaseModel):
    entity_name: str = Field(max_length=160)
    fact_type: str = Field(max_length=80)
    summary: str = Field(max_length=500)
    applicable_date: date | None = None
    source_url: str = Field(max_length=2000)
    source_title: str = Field(max_length=240)


class _CitedPlace(BaseModel):
    """A source-backed POI used only when the dedicated place provider is empty."""

    place_name: str = Field(max_length=160)
    stop_name: str = Field(max_length=160)
    category: str = Field(max_length=80)
    source_url: str = Field(max_length=2000)
    source_title: str = Field(max_length=240)
    is_primary_destination: bool = False


class _CitedIntercityOption(BaseModel):
    origin_name: str = Field(max_length=160)
    destination_name: str = Field(max_length=160)
    mode: str = Field(max_length=40)
    minimum_duration_minutes: int = Field(ge=1)
    maximum_duration_minutes: int = Field(ge=1)
    minimum_cost: float = Field(ge=0)
    maximum_cost: float = Field(ge=0)
    currency_code: str = Field(min_length=3, max_length=3)
    basis: str = Field(max_length=500)
    source_url: str = Field(max_length=2000)
    source_title: str = Field(max_length=240)


class _CitedCostBaseline(BaseModel):
    stop_name: str = Field(max_length=160)
    category: Literal["meal", "lodging", "local_transport"]
    minimum: float = Field(ge=0)
    maximum: float = Field(ge=0)
    currency_code: str = Field(min_length=3, max_length=3)
    unit: str = Field(max_length=120)
    source_url: str = Field(max_length=2000)
    source_title: str = Field(max_length=240)


class _CitedVisitDuration(BaseModel):
    place_name: str = Field(max_length=160)
    stop_name: str = Field(max_length=160)
    minimum_minutes: int = Field(ge=15, le=720)
    recommended_minutes: int = Field(ge=15, le=720)
    maximum_minutes: int = Field(ge=15, le=720)
    access_minutes: int | None = Field(default=None, ge=0, le=180)
    waiting_minutes: int | None = Field(default=None, ge=0, le=360)
    rest_minutes: int | None = Field(default=None, ge=0, le=180)
    exit_minutes: int | None = Field(default=None, ge=0, le=120)
    physical_effort: Literal["low", "moderate", "high"] | None = None
    last_entry_minutes_before_close: int | None = Field(
        default=None,
        ge=0,
        le=240,
    )
    confidence: Literal["low", "medium", "high"] = "medium"
    basis: str = Field(max_length=500)
    source_url: str = Field(max_length=2000)
    source_title: str = Field(max_length=240)


class _WebResearchResult(BaseModel):
    place_candidates: list[_CitedPlace] = Field(default_factory=list, max_length=24)
    facts: list[_CitedFact] = Field(default_factory=list, max_length=20)
    intercity_options: list[_CitedIntercityOption] = Field(
        default_factory=list,
        max_length=16,
    )
    cost_baselines: list[_CitedCostBaseline] = Field(
        default_factory=list,
        max_length=24,
    )
    visit_durations: list[_CitedVisitDuration] = Field(
        default_factory=list,
        max_length=20,
    )


class OpenAIWebSearchProvider:
    """Use hosted web search, then reject records not tied to a consulted URL."""

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_search_calls: int = 8,
    ) -> None:
        self._model = model
        # Do not inherit the machine-wide proxy settings. On this workstation
        # they point to an unavailable local proxy; a direct HTTPS connection
        # is the known-good path to OpenAI.
        self._http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=False,
        )
        self._client = AsyncOpenAI(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=0,
            http_client=self._http_client,
        )
        self._sources: dict[str, SourceRef] = {}
        self._max_search_calls = max(1, max_search_calls)

    async def research(
        self,
        context: PlanningContext,
        destination: ResolvedDestination,
        places: list[PlaceEvidence],
    ) -> CurrentTravelResearch:
        request = {
            "model": self._model,
            "instructions": _INSTRUCTIONS,
            "input": json.dumps(
                _research_input(context, destination, places),
                ensure_ascii=False,
                default=str,
            ),
            "tools": cast(
                Any,
                [
                    {
                        "type": "web_search",
                        "search_context_size": "medium",
                        "filters": {
                            "blocked_domains": [
                                "reddit.com",
                                "quora.com",
                                "pinterest.com",
                            ]
                        },
                    }
                ],
            ),
            "include": ["web_search_call.action.sources"],
            "text_format": _WebResearchResult,
            "max_tool_calls": self._max_search_calls,
            "max_output_tokens": 5000,
            "reasoning": {"effort": "medium"},
        }
        # Current-facts research is advisory. Bound retries so a provider
        # outage cannot trap an otherwise feasible itinerary for minutes.
        for attempt in range(2):
            try:
                response = await self._client.responses.parse(**request)
                break
            except (
                APIConnectionError,
                APITimeoutError,
                InternalServerError,
                RateLimitError,
            ):
                if attempt == 1:
                    raise
                # The same request is retried, so a successful retry returns
                # the same evidence contract rather than a degraded fallback.
                await sleep(1)
        parsed = response.output_parsed
        if parsed is None:
            raise RuntimeError("Web research returned no structured result.")
        consulted = _consulted_urls(response.output)
        if not consulted:
            raise RuntimeError("Web research returned no consulted source URLs.")
        return self._normalize(parsed, consulted, context, destination, places)

    def _normalize(
        self,
        result: _WebResearchResult,
        consulted: set[str],
        context: PlanningContext,
        destination: ResolvedDestination,
        places: list[PlaceEvidence],
    ) -> CurrentTravelResearch:
        facts: list[WebFactEvidence] = []
        fallback_places: list[PlaceEvidence] = []
        options: list[IntercityOptionEvidence] = []
        baselines: list[CostBaselineEvidence] = []
        durations: list[VisitDurationEvidence] = []
        labels = [context.origin, *(stop.name for stop in destination.stops)]
        if context.end_location:
            labels.append(context.end_location)
        location_aliases = _location_aliases(labels)
        stop_names = {stop.name for stop in destination.stops}
        stop_aliases = _location_aliases(list(stop_names))
        place_names = {place.name.casefold(): place.name for place in places}
        expected_pairs = {
            (origin, target)
            for origin, target in zip(labels, labels[1:], strict=False)
            if origin.casefold() != target.casefold()
        }
        allowed_modes = {_normalized_mode(mode) for mode in context.long_distance_modes}
        allowed_modes.add("flight")

        existing_place_stops = {place.stop_name.casefold() for place in places}
        for place_record in result.place_candidates:
            stop_name = _canonical_location(place_record.stop_name, stop_aliases)
            if stop_name is None or stop_name.casefold() in existing_place_stops:
                continue
            source_id = self._register_source(
                place_record.source_url,
                place_record.source_title,
                consulted,
            )
            if source_id is None:
                continue
            normalized_name = place_record.place_name.strip()
            if not normalized_name:
                continue
            fallback_places.append(
                PlaceEvidence(
                    id="web-place-"
                    + _short_hash(stop_name + normalized_name + source_id),
                    name=normalized_name,
                    stop_name=stop_name,
                    categories=[place_record.category.strip() or "attraction"],
                    # The source URL is a stable provider identity for this
                    # evidence record. Coordinates remain intentionally absent
                    # and are surfaced as a planning warning, not invented.
                    provider_place_id="web:" + _short_hash(normalized_name + source_id),
                    status=FactStatus.ESTIMATED,
                    source_ids=[source_id],
                    is_primary_destination=place_record.is_primary_destination,
                )
            )

        for fact_record in result.facts:
            entity_name = place_names.get(fact_record.entity_name.casefold())
            if entity_name is None:
                entity_name = _canonical_location(
                    fact_record.entity_name,
                    stop_aliases,
                )
            if entity_name is None:
                continue
            source_id = self._register_source(
                fact_record.source_url,
                fact_record.source_title,
                consulted,
            )
            if source_id is None:
                continue
            facts.append(
                WebFactEvidence(
                    id=(
                        "web-fact-"
                        + _short_hash(
                            fact_record.entity_name + fact_record.fact_type + source_id
                        )
                    ),
                    entity_name=entity_name,
                    fact_type=fact_record.fact_type,
                    summary=fact_record.summary,
                    applicable_date=fact_record.applicable_date,
                    status=FactStatus.ESTIMATED,
                    source_ids=[source_id],
                )
            )

        for option_record in result.intercity_options:
            origin_name = _canonical_location(
                option_record.origin_name,
                location_aliases,
            )
            destination_name = _canonical_location(
                option_record.destination_name,
                location_aliases,
            )
            if origin_name is None or destination_name is None:
                continue
            pair = (origin_name, destination_name)
            if pair not in expected_pairs or (
                context.long_distance_modes
                and _normalized_mode(option_record.mode) not in allowed_modes
            ):
                continue
            source_id = self._register_source(
                option_record.source_url,
                option_record.source_title,
                consulted,
            )
            if (
                source_id is None
                or option_record.minimum_duration_minutes
                > option_record.maximum_duration_minutes
                or option_record.maximum_duration_minutes > 72 * 60
                or option_record.minimum_cost > option_record.maximum_cost
            ):
                continue
            options.append(
                IntercityOptionEvidence(
                    id=(
                        "web-intercity-"
                        + _short_hash(
                            option_record.origin_name
                            + option_record.destination_name
                            + option_record.mode
                            + source_id
                        )
                    ),
                    origin_name=origin_name,
                    destination_name=destination_name,
                    mode=option_record.mode.casefold(),
                    minimum_duration_minutes=option_record.minimum_duration_minutes,
                    maximum_duration_minutes=option_record.maximum_duration_minutes,
                    minimum_cost=Decimal(str(option_record.minimum_cost)),
                    maximum_cost=Decimal(str(option_record.maximum_cost)),
                    currency_code=option_record.currency_code.upper(),
                    basis=option_record.basis,
                    status=FactStatus.ESTIMATED,
                    source_ids=[source_id],
                )
            )

        for baseline_record in result.cost_baselines:
            stop_name = _canonical_location(baseline_record.stop_name, stop_aliases)
            if stop_name is None:
                continue
            source_id = self._register_source(
                baseline_record.source_url,
                baseline_record.source_title,
                consulted,
            )
            if (
                source_id is None
                or baseline_record.minimum > baseline_record.maximum
                or not _valid_baseline_unit(
                    baseline_record.category,
                    baseline_record.unit,
                )
            ):
                continue
            baselines.append(
                CostBaselineEvidence(
                    id=(
                        "web-cost-"
                        + _short_hash(
                            baseline_record.stop_name
                            + baseline_record.category
                            + source_id
                        )
                    ),
                    stop_name=stop_name,
                    category=baseline_record.category,
                    minimum=Decimal(str(baseline_record.minimum)),
                    maximum=Decimal(str(baseline_record.maximum)),
                    currency_code=baseline_record.currency_code.upper(),
                    unit=baseline_record.unit,
                    status=FactStatus.ESTIMATED,
                    source_ids=[source_id],
                )
            )

        place_lookup = {
            (place.name.casefold(), place.stop_name.casefold()): place
            for place in places
        }
        for duration_record in result.visit_durations:
            duration_stop = _canonical_location(
                duration_record.stop_name,
                stop_aliases,
            )
            if duration_stop is None:
                continue
            place = place_lookup.get(
                (
                    duration_record.place_name.casefold(),
                    duration_stop.casefold(),
                )
            )
            if place is None or not (
                duration_record.minimum_minutes
                <= duration_record.recommended_minutes
                <= duration_record.maximum_minutes
            ):
                continue
            source_id = self._register_source(
                duration_record.source_url,
                duration_record.source_title,
                consulted,
            )
            if source_id is None:
                continue
            durations.append(
                VisitDurationEvidence(
                    id=f"web-duration-{_short_hash(place.id + source_id)}",
                    place_id=place.id,
                    minimum_minutes=duration_record.minimum_minutes,
                    recommended_minutes=duration_record.recommended_minutes,
                    maximum_minutes=duration_record.maximum_minutes,
                    access_minutes=duration_record.access_minutes,
                    waiting_minutes=duration_record.waiting_minutes,
                    rest_minutes=duration_record.rest_minutes,
                    exit_minutes=duration_record.exit_minutes,
                    physical_effort=duration_record.physical_effort,
                    last_entry_minutes_before_close=(
                        duration_record.last_entry_minutes_before_close
                    ),
                    confidence=duration_record.confidence,
                    basis=duration_record.basis,
                    status=FactStatus.ESTIMATED,
                    source_ids=[source_id],
                )
            )
        return CurrentTravelResearch(
            places=fallback_places,
            web_facts=facts,
            intercity_options=options,
            cost_baselines=baselines,
            visit_durations=durations,
        )

    def _register_source(
        self,
        url: str,
        title: str,
        consulted: set[str],
    ) -> str | None:
        normalized = _normalize_url(url)
        if normalized not in consulted:
            return None
        source_id = f"src-openai-web-{_short_hash(normalized)}"
        self._sources[source_id] = SourceRef(
            id=source_id,
            provider="openai-web-search",
            kind="current-travel-evidence",
            title=title.strip() or urlsplit(normalized).netloc,
            url=normalized,
            retrieved_at=datetime.now(UTC),
        )
        return source_id

    def sources(self) -> list[SourceRef]:
        return list(self._sources.values())

    async def close(self) -> None:
        await self._client.close()


_INSTRUCTIONS = """Research current trip-planning evidence using web search.
Return only facts supported by the exact source_url you consulted. Prefer official venue,
government tourism, public transport operator, airport, rail operator, or reputable current
cost sources. Omit a record when a useful source or numeric range is unavailable. Never
invent a service number, timetable, fare, hotel availability, or booking. Intercity options
must be broad mode-level door-to-door duration and total traveller-cost ranges. Meal costs
are per adult per meal, lodging costs are per room per night, and local transport costs are
for the complete traveller group per typical five-kilometre trip. All costs must use the
source's currency. Facts should cover material seasonal timing, safety or access warnings,
local practical rules, and date-specific closures, entry restrictions, last-entry rules, or ticket access
that a traveller should confirm. Return a visit-duration record only when a consulted source
explicitly supports the time needed; do not create a duration from general knowledge. Keep
summaries short and factual. For each exact core POI, also look for material operational
time: entry/access procedure, usual or date-relevant waiting/queue time, recovery/rest
time, exit time, physical effort, and any explicit last-entry rule. Populate an operational
field only when the consulted source supports it; otherwise return null. Confidence must
describe the strength and specificity of the consulted evidence. Apply this policy to every
venue type and never assume rules merely because a place belongs to a particular category.
If core_pois is empty for any stop, return at least four exact, visitor-relevant place_candidates
for that stop from consulted official, tourism, museum, or similarly reliable sources. A
place candidate must be a real named place, not a neighbourhood, generic activity, hotel,
or restaurant. Set is_primary_destination true for the most central landmark only when the
source supports that choice."""


def _research_input(
    context: PlanningContext,
    destination: ResolvedDestination,
    places: list[PlaceEvidence],
) -> dict[str, Any]:
    spending_tier = context.requirements.get("spending_tier")
    labels = [context.origin, *(stop.name for stop in destination.stops)]
    if context.end_location:
        labels.append(context.end_location)
    return {
        "trip_dates": {"start": context.start_date, "end": context.end_date},
        "origin": context.origin,
        "end_location": context.end_location,
        "traveller_count": context.traveller_count,
        "spending_tier": spending_tier,
        "allowed_long_distance_modes": context.long_distance_modes,
        "stops": [stop.model_dump(mode="json") for stop in destination.stops],
        "required_intercity_pairs": [
            {"origin": origin, "destination": target}
            for origin, target in zip(labels, labels[1:], strict=False)
            if origin.casefold() != target.casefold()
        ],
        "required_cost_stops": [stop.name for stop in destination.stops],
        "core_pois": [
            {
                "name": place.name,
                "stop": place.stop_name,
                "address": place.address,
                "provider_place_id": place.provider_place_id,
                "published_opening_hours": place.opening_hours,
            }
            for place in _representative_places(places, destination)
        ],
    }


def _representative_places(
    places: list[PlaceEvidence],
    destination: ResolvedDestination,
) -> list[PlaceEvidence]:
    """Give web research compact POI coverage for every stop, not one long prefix."""

    selected: list[PlaceEvidence] = []
    for stop in destination.stops:
        stop_places = [
            place
            for place in places
            if place.stop_name.casefold() == stop.name.casefold()
        ]
        selected.extend(stop_places[:2])
        if len(selected) >= 16:
            break
    return selected[:16]


def _consulted_urls(output: list[Any]) -> set[str]:
    urls: set[str] = set()
    for item in output:
        if getattr(item, "type", None) != "web_search_call":
            continue
        action = getattr(item, "action", None)
        for source in getattr(action, "sources", None) or []:
            url = getattr(source, "url", None)
            if isinstance(url, str):
                urls.add(_normalize_url(url))
        action_url = getattr(action, "url", None)
        if isinstance(action_url, str):
            urls.add(_normalize_url(action_url))
    return urls


def _normalize_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    query = urlencode(
        [
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_")
        ]
    )
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit(
        (parsed.scheme.casefold(), parsed.netloc.casefold(), path, query, "")
    )


def _short_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:16]


def _normalized_mode(value: str) -> str:
    normalized = value.casefold()
    return {
        "air": "flight",
        "plane": "flight",
        "rail": "train",
        "driving": "car",
    }.get(normalized, normalized)


def _valid_baseline_unit(category: str, unit: str) -> bool:
    normalized = unit.casefold()
    if category == "meal":
        return "person" in normalized and "meal" in normalized
    if category == "lodging":
        return "room" in normalized and "night" in normalized
    return "trip" in normalized and ("five" in normalized or "5" in normalized)


def _location_aliases(labels: list[str]) -> dict[str, str | None]:
    aliases: dict[str, str | None] = {}
    for label in labels:
        for alias in {_location_key(label), _location_key(label.split(",", 1)[0])}:
            existing = aliases.get(alias)
            if existing is not None and existing.casefold() != label.casefold():
                aliases[alias] = None
            elif alias not in aliases:
                aliases[alias] = label
    return aliases


def _canonical_location(
    value: str,
    aliases: dict[str, str | None],
) -> str | None:
    return aliases.get(_location_key(value))


def _location_key(value: str) -> str:
    return " ".join(value.casefold().strip().split())
