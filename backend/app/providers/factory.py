"""Construct the live provider bundle used by the application."""

from __future__ import annotations

from app.providers.tool_loop import OpenAIResponsesToolModel
from app.providers.base import ProviderBundle
from app.providers.currency import FrankfurterProvider
from app.providers.google_destination import GoogleDestinationResolver
from app.providers.google_places import GooglePlacesProvider
from app.providers.weather import OpenMeteoProvider
from app.providers.openai_itinerary import OpenAIItineraryProvider
from app.providers.tavily_search import TavilyCurrentFactsProvider
from app.providers.openroute import OpenRouteServiceProvider
from app.providers.images import (
    ImageEnrichedPlaceProvider,
    WikimediaImageProvider,
)
from app.config import Settings


def build_providers(settings: Settings) -> ProviderBundle:
    if settings.mode != "live":
        raise RuntimeError(
            "Synthetic provider modes are disabled in the application runtime."
        )

    ors = OpenRouteServiceProvider(
        api_key=settings.ors_api_key or "",
        timeout_seconds=min(settings.timeout_seconds, 30),
    )
    destination = GoogleDestinationResolver(
        api_key=settings.google_maps_api_key or "",
        timeout_seconds=min(settings.timeout_seconds, 30),
    )
    places = ImageEnrichedPlaceProvider(
        GooglePlacesProvider(
            api_key=settings.google_maps_api_key or "",
            timeout_seconds=min(settings.timeout_seconds, 30),
        ),
        WikimediaImageProvider(timeout_seconds=min(settings.timeout_seconds, 30)),
    )
    weather = OpenMeteoProvider(
        geocode=destination.geocode,
        timeout_seconds=min(settings.timeout_seconds, 30),
    )
    currency = FrankfurterProvider(timeout_seconds=min(settings.timeout_seconds, 30))
    # Current-facts research is advisory; it must not inherit the full
    # end-to-end planner timeout when a web-search call stalls.
    # Do not impose a separate agent deadline. The outer planning-job timeout
    # remains the only operational guardrail, while a user trip gets one
    # complete research/compiler pass rather than hidden retry loops.
    agent_timeout_seconds = settings.timeout_seconds
    current_facts_timeout_seconds = min(settings.timeout_seconds, 45)
    itinerary = OpenAIItineraryProvider(
        api_key=settings.openai_api_key or "",
        model=settings.openai_model,
        timeout_seconds=agent_timeout_seconds,
    )
    agent_model = OpenAIResponsesToolModel(
        api_key=settings.openai_api_key or "",
        model=settings.openai_model,
        timeout_seconds=agent_timeout_seconds,
    )
    current_facts = (
        TavilyCurrentFactsProvider(
            tavily_api_key=settings.tavily_api_key,
            openai_api_key=settings.openai_api_key or "",
            model=settings.openai_model,
            timeout_seconds=current_facts_timeout_seconds,
            max_search_calls=settings.max_search_calls_per_trip or 6,
        )
        if settings.tavily_api_key
        else None
    )
    return ProviderBundle(
        destination=destination,
        places=places,
        routes=ors,
        weather=weather,
        currency=currency,
        itinerary=itinerary,
        agent_model=agent_model,
        current_facts=current_facts,
    )


async def close_providers(bundle: ProviderBundle) -> None:
    seen: set[int] = set()
    for provider in (
        bundle.destination,
        bundle.places,
        bundle.routes,
        bundle.weather,
        bundle.currency,
        bundle.itinerary,
        bundle.agent_model,
        bundle.current_facts,
    ):
        if provider is None:
            continue
        if id(provider) in seen:
            continue
        seen.add(id(provider))
        close = getattr(provider, "close", None)
        if close is not None:
            await close()
