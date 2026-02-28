"""Server-side, lazy Google Place photo resolver; never persist its output."""

from __future__ import annotations
from typing import Any
import httpx


class GooglePlaceImageResolver:
    """Resolve transient image URLs and their provider attributions."""

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        self.api_key, self.client = api_key, client or httpx.AsyncClient()

    async def resolve(self, google_place_id: str) -> dict[str, Any]:
        details = await self.client.get(
            f"https://places.googleapis.com/v1/places/{google_place_id}",
            headers={"X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": "photos"},
        )
        if details.status_code >= 400:
            return {"status": "unavailable"}
        photos = details.json().get("photos") or []
        photo = next(
            (item for item in photos if item.get("widthPx") and item.get("heightPx")),
            None,
        )
        if not photo:
            return {"status": "unavailable"}
        # The photo resource name remains transient in this request only.
        media = await self.client.get(
            f"https://places.googleapis.com/v1/{photo['name']}/media",
            headers={"X-Goog-Api-Key": self.api_key},
            params={"maxHeightPx": 800, "skipHttpRedirect": "true"},
        )
        if media.status_code >= 400:
            return {"status": "unavailable"}
        payload = media.json()
        return {
            "status": "available",
            "photo_uri": payload.get("photoUri"),
            "author_attributions": photo.get("authorAttributions", []),
        }
