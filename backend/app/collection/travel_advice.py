"""Bounded, cached current-facts lookup for explicit Phase 1 advice questions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Protocol

import httpx

from app.models.memory import TripMemory

logger = logging.getLogger(__name__)
_CACHE_TTL = timedelta(hours=24)


class TravelAdviceLookup(Protocol):
    async def lookup(self, memory: TripMemory, question: str) -> list[str]: ...

    async def close(self) -> None: ...


class TavilyTravelAdviceLookup:
    """Use Tavily only when a traveller explicitly needs changing information.

    The cache is deliberately process-local because PostgreSQL remains the
    application source of truth. A missed cache never blocks the conversation;
    the existing destination-intelligence facts still reach the reply agent.
    """

    def __init__(self, tavily_api_key: str | None) -> None:
        self._api_key = tavily_api_key
        self._client = httpx.AsyncClient(timeout=15.0, trust_env=False)
        self._cache: dict[tuple[str, str], tuple[datetime, list[str]]] = {}

    async def lookup(self, memory: TripMemory, question: str) -> list[str]:
        if not self._api_key or not _needs_current_facts(question):
            return []
        destination = memory.needed.destination
        if destination is None or not destination.items:
            return []
        month = (
            memory.needed.start_date.strftime("%B %Y")
            if memory.needed.start_date
            else "upcoming travel"
        )
        destination_text = ", ".join(destination.items)
        cache_key = (destination_text.casefold(), month.casefold())
        cached = self._cache.get(cache_key)
        now = datetime.now(timezone.utc)
        if cached and now - cached[0] < _CACHE_TTL:
            return cached[1]

        query = (
            f"{destination_text} travel {month}: official tourism advice, seasonal weather, "
            f"local access and practical visitor information. Traveller question: {question}"
        )
        try:
            response = await self._client.post(
                "https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"query": query, "search_depth": "basic", "max_results": 4},
            )
            response.raise_for_status()
            results = response.json().get("results", [])
        except (httpx.HTTPError, ValueError) as exc:
            logger.info(
                "Advice lookup unavailable; using saved destination context: %s", exc
            )
            return []

        context = [
            f"{item.get('title', 'Travel information')}: {item.get('content', '').strip()}"
            for item in results
            if item.get("content")
        ]
        self._cache[cache_key] = (now, context)
        return context

    async def close(self) -> None:
        await self._client.aclose()


def _needs_current_facts(question: str) -> bool:
    text = question.casefold()
    changing_terms = (
        "weather",
        "season",
        "november",
        "december",
        "best time",
        "current",
        "open",
        "closed",
        "schedule",
        "visa",
        "advisory",
        "safe",
        "price",
        "available",
        "rain",
        "monsoon",
    )
    return any(term in text for term in changing_terms)
