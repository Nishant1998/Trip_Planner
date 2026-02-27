"""Tavily-backed current-facts research for the Phase 2 planner.

Tavily supplies web results. OpenAI only transforms supplied snippets into the
existing structured evidence contract; it never receives a web-search tool.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.models.context import PlanningContext, ResolvedDestination
from app.models.evidence import CurrentTravelResearch, PlaceEvidence
from app.providers.openai_search import (
    OpenAIWebSearchProvider,
    _INSTRUCTIONS,
    _WebResearchResult,
)


class TavilyCurrentFactsProvider(OpenAIWebSearchProvider):
    """Turn bounded Tavily results into source-linked planning evidence."""

    def __init__(
        self,
        *,
        tavily_api_key: str,
        openai_api_key: str,
        model: str,
        timeout_seconds: float,
        max_search_calls: int = 8,
    ) -> None:
        super().__init__(
            api_key=openai_api_key,
            model=model,
            timeout_seconds=timeout_seconds,
            max_search_calls=max_search_calls,
        )
        self._tavily_api_key = tavily_api_key

    async def research(
        self,
        context: PlanningContext,
        destination: ResolvedDestination,
        places: list[PlaceEvidence],
    ) -> CurrentTravelResearch:
        results = await self._search_destination_facts(context, destination)
        if not results:
            raise RuntimeError("Tavily returned no usable destination research.")
        consulted = {item["url"] for item in results if item.get("url")}
        payload = {
            "trip": {
                "origin": context.origin,
                "dates": [str(context.start_date), str(context.end_date)],
                "currency": context.currency_code,
                "budget": str(context.trip_budget),
                "travellers": context.traveller_count,
                "interests": context.activity_interests,
                "must_see": context.must_see,
                "accessibility_needs": context.accessibility_needs,
                "dietary_restrictions": context.dietary_restrictions,
            },
            "resolved_stops": [stop.name for stop in destination.stops],
            "known_places": [
                {"name": place.name, "stop_name": place.stop_name} for place in places
            ],
            "tavily_sources": results,
        }
        request: dict[str, Any] = {
            "model": self._model,
            "instructions": (
                _INSTRUCTIONS
                + "\nUse only the supplied Tavily source snippets. Do not browse, "
                "invent a source, or cite a URL outside tavily_sources."
            ),
            "input": json.dumps(payload, ensure_ascii=False, default=str),
            "text_format": _WebResearchResult,
            "max_output_tokens": 3500,
            "reasoning": {"effort": "low"},
        }
        for attempt in range(2):
            try:
                response = await self._client.responses.parse(**request)
                break
            except Exception:
                if attempt:
                    raise
                await asyncio.sleep(1)
        parsed = response.output_parsed
        if parsed is None:
            raise RuntimeError("Research extraction returned no structured result.")
        return self._normalize(parsed, consulted, context, destination, places)

    async def _search_destination_facts(
        self,
        context: PlanningContext,
        destination: ResolvedDestination,
    ) -> list[dict[str, str]]:
        stop_names = [stop.name for stop in destination.stops][: self._max_search_calls]
        queries = [
            (
                f"{stop} official tourism opening hours travel information "
                f"{context.start_date.isoformat()} {context.end_date.isoformat()}"
            )
            for stop in stop_names
        ]
        responses = await asyncio.gather(
            *(self._tavily_search(query) for query in queries)
        )
        deduped: dict[str, dict[str, str]] = {}
        for response in responses:
            for item in response:
                if item.get("url"):
                    deduped.setdefault(item["url"], item)
        return list(deduped.values())[: max(5, self._max_search_calls * 5)]

    async def _tavily_search(self, query: str) -> list[dict[str, str]]:
        response = await self._http_client.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {self._tavily_api_key}"},
            json={
                "query": query,
                "search_depth": "basic",
                "max_results": 5,
                "include_answer": False,
            },
        )
        response.raise_for_status()
        payload = response.json()
        return [
            {
                "title": str(item.get("title", ""))[:240],
                "url": str(item.get("url", ""))[:2000],
                "content": str(item.get("content", ""))[:1200],
            }
            for item in payload.get("results", [])
            if item.get("url")
        ]
