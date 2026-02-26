"""Optional, identity-safe Wikimedia images for provider-backed places."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlparse

from app.models.evidence import PlaceEvidence, SourceRef
from app.models.context import PlanningContext, ResolvedStop
from app.providers.base import PlaceProvider
from app.providers.http import ProviderHttpClient


class WikimediaImageProvider:
    """Attach an image only when Wikipedia resolves the exact place title."""

    API_URL = "https://en.wikipedia.org/w/api.php"

    def __init__(self, timeout_seconds: float = 20) -> None:
        self._http = ProviderHttpClient(timeout_seconds)
        self._sources: dict[str, SourceRef] = {}

    async def enrich(self, places: list[PlaceEvidence]) -> list[PlaceEvidence]:
        names = list(dict.fromkeys(place.name for place in places))
        if not names:
            return places
        response = await self._http.get(
            self.API_URL,
            headers={"Api-User-Agent": "RiyaTripPlanner-PlaceImages/0.1"},
            params={
                "action": "query",
                "titles": "|".join(names),
                "redirects": 1,
                "prop": "pageimages",
                "piprop": "thumbnail",
                "pithumbsize": 960,
                "format": "json",
            },
        )
        query = response.json().get("query") or {}
        aliases = _aliases(query)
        pages = {
            _normalized(str(page.get("title") or "")): page
            for page in (query.get("pages") or {}).values()
            if isinstance(page, dict) and "missing" not in page
        }
        return [self._enrich_one(place, aliases, pages) for place in places]

    def _enrich_one(
        self,
        place: PlaceEvidence,
        aliases: dict[str, str],
        pages: dict[str, dict[str, Any]],
    ) -> PlaceEvidence:
        title = _resolve_alias(place.name, aliases)
        page = pages.get(_normalized(title))
        if page is None:
            return place
        thumbnail = page.get("thumbnail") or {}
        image_url = str(thumbnail.get("source") or "")
        if not _safe_wikimedia_image(image_url):
            return place
        page_id = str(page.get("pageid") or "")
        canonical_title = str(page.get("title") or title)
        if not page_id:
            return place
        source_id = f"src-wikipedia-image-{page_id}"
        self._sources[source_id] = SourceRef(
            id=source_id,
            provider="wikipedia",
            kind="image",
            title=f"Wikipedia image source: {canonical_title}",
            url=f"https://en.wikipedia.org/wiki/{quote(canonical_title.replace(' ', '_'))}",
            provider_record_id=page_id,
            retrieved_at=datetime.now(UTC),
        )
        return place.model_copy(
            update={
                "image_url": image_url,
                "source_ids": [*place.source_ids, source_id],
            }
        )

    def sources(self) -> list[SourceRef]:
        return list(self._sources.values())

    async def close(self) -> None:
        await self._http.close()


class ImageEnrichedPlaceProvider:
    """Keep place identity and optional image lookup as separate adapters."""

    def __init__(
        self,
        places: PlaceProvider,
        images: WikimediaImageProvider,
    ) -> None:
        self._places = places
        self._images = images

    async def search_places(
        self,
        stop: ResolvedStop,
        context: PlanningContext,
    ) -> list[PlaceEvidence]:
        places = await self._places.search_places(stop, context)
        try:
            return await self._images.enrich(places)
        except Exception:
            return places

    def sources(self) -> list[SourceRef]:
        records: dict[str, SourceRef] = {}
        for provider in (self._places, self._images):
            source_method = getattr(provider, "sources", None)
            if source_method is not None:
                records.update({source.id: source for source in source_method()})
        return list(records.values())

    async def close(self) -> None:
        for provider in (self._places, self._images):
            close = getattr(provider, "close", None)
            if close is not None:
                await close()


def _aliases(query: dict[str, Any]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for group in ("normalized", "redirects"):
        for record in query.get(group) or []:
            source = str(record.get("from") or "")
            target = str(record.get("to") or "")
            if source and target:
                aliases[_normalized(source)] = target
    return aliases


def _resolve_alias(title: str, aliases: dict[str, str]) -> str:
    resolved = title
    for _ in range(3):
        replacement = aliases.get(_normalized(resolved))
        if replacement is None:
            break
        resolved = replacement
    return resolved


def _normalized(value: str) -> str:
    return " ".join(value.casefold().replace("_", " ").split())


def _safe_wikimedia_image(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == "upload.wikimedia.org"
