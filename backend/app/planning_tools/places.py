"""Google Places (New) search, details, and photo metadata tools."""

from __future__ import annotations

from urllib.parse import quote

import httpx
from typing import Protocol

from .contracts import NearbySearchInput, Photo, Place, PlaceLookupInput, PlaceSearchInput
from .http import HttpTool, failure, result, unavailable

BASE = "https://places.googleapis.com/v1"
SEARCH_FIELDS = (
    "places.id,places.displayName,places.location,places.formattedAddress,"
    "places.types,places.googleMapsUri,places.businessStatus"
)
DETAIL_FIELDS = (
    "id,displayName,location,formattedAddress,types,googleMapsUri,"
    "regularOpeningHours,photos,businessStatus"
)


class PlacesProvider(Protocol):
    """Future providers can implement this without changing the agent interface."""

    async def lookup(self, request: PlaceLookupInput): ...
    async def close(self) -> None: ...


def _place(raw: dict) -> Place:
    position = raw.get("location")
    return Place(
        place_id=raw["id"],
        name=(raw.get("displayName") or {}).get("text") or raw["id"],
        location={"latitude": position["latitude"], "longitude": position["longitude"]}
        if position and "latitude" in position and "longitude" in position else None,
        address=raw.get("formattedAddress"),
        types=raw.get("types") or [],
        map_url=raw.get("googleMapsUri"),
        opening_hours=raw.get("regularOpeningHours"),
        business_status=raw.get("businessStatus"),
        photo_names=[p["name"] for p in raw.get("photos") or [] if p.get("name")],
        photos=[{"name": p["name"], "attribution": p.get("authorAttributions") or []}
                for p in raw.get("photos") or [] if p.get("name")],
    )


class PlacesTools(HttpTool):
    def __init__(self, api_key: str | None, client: httpx.AsyncClient | None = None):
        super().__init__(client)
        self.api_key = api_key

    def _headers(self, fields: str) -> dict[str, str]:
        return {"X-Goog-Api-Key": self.api_key or "", "X-Goog-FieldMask": fields}

    async def lookup(self, request: PlaceLookupInput):
        """Single agent-facing entrypoint; never fan out into extra paid calls."""
        request = PlaceLookupInput.model_validate(request)
        if request.action == "text":
            return await self.text_search(PlaceSearchInput(
                query=request.query, max_results=request.max_results,
                center=request.center, radius_m=request.radius_m,
            ))
        if request.action == "nearby":
            return await self.nearby_search(NearbySearchInput(
                center=request.center, radius_m=request.radius_m,
                included_types=request.included_types, max_results=request.max_results,
            ))
        if request.action == "details":
            return await self.details(request.place_id)
        return await self.photo(request.photo_name, request.max_width_px)

    async def text_search(self, query: PlaceSearchInput):
        if not self.api_key:
            return unavailable("google_places", "Google Places API key is not configured.")
        body = {"textQuery": query.query, "maxResultCount": query.max_results}
        if query.center and query.radius_m:
            body["locationBias"] = {"circle": {
                "center": {"latitude": query.center.latitude, "longitude": query.center.longitude},
                "radius": query.radius_m,
            }}
        return await self._search("places:searchText", body)

    async def nearby_search(self, query: NearbySearchInput):
        if not self.api_key:
            return unavailable("google_places", "Google Places API key is not configured.")
        body = {
            "includedTypes": query.included_types, "maxResultCount": query.max_results,
            "locationRestriction": {"circle": {
                "center": {"latitude": query.center.latitude, "longitude": query.center.longitude},
                "radius": query.radius_m,
            }},
        }
        return await self._search("places:searchNearby", body)

    async def _search(self, endpoint: str, body: dict):
        try:
            response = await self.client.post(
                f"{BASE}/{endpoint}", json=body, headers=self._headers(SEARCH_FIELDS)
            )
            response.raise_for_status()
            payload = response.json()
            places = [_place(item) for item in payload.get("places") or []]
            return result(places, "google_places")
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            return failure("google_places", exc)

    async def details(self, place_id: str):
        if not self.api_key:
            return unavailable("google_places", "Google Places API key is not configured.")
        try:
            response = await self.client.get(
                f"{BASE}/places/{quote(place_id, safe='')}",
                headers=self._headers(DETAIL_FIELDS),
            )
            response.raise_for_status()
            return result(_place(response.json()), "google_places")
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            return failure("google_places", exc)

    async def photo(self, photo_name: str, max_width_px: int = 800):
        if not self.api_key:
            return unavailable("google_places", "Google Places API key is not configured.")
        if not 1 <= max_width_px <= 4800 or not photo_name.startswith("places/") or "/photos/" not in photo_name:
            return failure("google_places", ValueError("invalid photo reference"))
        try:
            response = await self.client.get(
                f"{BASE}/{quote(photo_name, safe='/')}/media",
                params={"maxWidthPx": max_width_px, "skipHttpRedirect": "true"},
                headers={"X-Goog-Api-Key": self.api_key},
            )
            response.raise_for_status()
            payload = response.json()
            photo = Photo(uri=payload.get("photoUri"), attribution=payload.get("authorAttributions") or [])
            return result(photo, "google_places", warnings=["Photo URL is unavailable."] if not photo.uri else [])
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            return failure("google_places", exc)
