"""Destination cover image resolver for trip cards."""

from __future__ import annotations

import asyncio
import os
from collections import OrderedDict
from time import monotonic

from app.planning_tools.contracts import Photo, Place, PlaceSearchInput
from app.planning_tools.places import PlacesTools

_cover_cache: OrderedDict[str, tuple[float, str, str | None, str | None]] = OrderedDict()


async def destination_cover(destination: str, saved: str | None) -> tuple[str, str | None, str | None]:
    """Resolve legacy generic covers without changing saved trip data."""
    if saved and saved != DEFAULT_TRAVEL_COVER:
        return saved, None, None
    key = destination.strip().casefold()
    cached = _cover_cache.get(key)
    if cached and cached[0] > monotonic():
        return cached[1], cached[2], cached[3]
    fallback = resolve_destination_cover_image(destination)
    if fallback != DEFAULT_TRAVEL_COVER:
        return fallback, None, None
    provider = PlacesTools(os.getenv("GOOGLE_MAPS_API_KEY") or os.getenv("GPLACES_API_KEY") or os.getenv("GOOGLE_API_KEY"))
    cover, source, credit = fallback, None, None
    try:
        async with asyncio.timeout(12):
            search = await provider.text_search(PlaceSearchInput(query=destination.strip(), max_results=1))
            if search.data:
                details = await provider.details(search.data[0].place_id)
                place = details.data
                if isinstance(place, Place) and place.photos:
                    ref = place.photos[0]
                    photo = await provider.photo(ref.name)
                    if isinstance(photo.data, Photo) and photo.data.uri:
                        cover, source = photo.data.uri, place.map_url
                        credit = ', '.join(a['displayName'] for a in ref.attribution if a.get('displayName'))
    except Exception:  # noqa: BLE001 - optional image enrichment
        pass  # A missing cover must not prevent access to saved trips.
    finally:
        await provider.close()
    _cover_cache[key] = (monotonic() + 600, cover, source, credit)
    while len(_cover_cache) > 128:
        _cover_cache.popitem(last=False)
    return cover, source, credit

# High-resolution, curated, optimized CDN destination images (w=800, quality=80)
_CURATED_DESTINATION_COVERS: dict[str, str] = {
    # Japan
    "japan": "https://images.unsplash.com/photo-1503899036084-c55cdd92da26?auto=format&fit=crop&w=800&q=80",
    "tokyo": "https://images.unsplash.com/photo-1540959733332-eab4deabeeaf?auto=format&fit=crop&w=800&q=80",
    "kyoto": "https://images.unsplash.com/photo-1493976040374-85c8e12f0c0e?auto=format&fit=crop&w=800&q=80",
    "osaka": "https://images.unsplash.com/photo-1590559899731-a382839e5549?auto=format&fit=crop&w=800&q=80",
    # India
    "goa": "https://images.unsplash.com/photo-1512343879784-a960bf40e7f2?auto=format&fit=crop&w=800&q=80",
    "jaipur": "https://images.unsplash.com/photo-1599661046289-e31897846e41?auto=format&fit=crop&w=800&q=80",
    "kerala": "https://images.unsplash.com/photo-1602216056096-3b40cc0c9944?auto=format&fit=crop&w=800&q=80",
    "munnar": "https://images.unsplash.com/photo-1588668214407-6ea9a6d8c272?auto=format&fit=crop&w=800&q=80",
    "udaipur": "https://images.unsplash.com/photo-1615836245337-f5b9b2303f10?auto=format&fit=crop&w=800&q=80",
    "manali": "https://images.unsplash.com/photo-1626621341517-bbf3d9990a23?auto=format&fit=crop&w=800&q=80",
    "ladakh": "https://images.unsplash.com/photo-1581793745862-99fde7fa73d2?auto=format&fit=crop&w=800&q=80",
    "varanasi": "https://images.unsplash.com/photo-1561361513-2d000a50f0dc?auto=format&fit=crop&w=800&q=80",
    "delhi": "https://images.unsplash.com/photo-1587474260584-136574528ed5?auto=format&fit=crop&w=800&q=80",
    "mumbai": "https://images.unsplash.com/photo-1570168007204-dfb528c6958f?auto=format&fit=crop&w=800&q=80",
    "bangalore": "https://images.unsplash.com/photo-1596176530529-78163a4f7af2?auto=format&fit=crop&w=800&q=80",
    # International Highlights
    "paris": "https://images.unsplash.com/photo-1502602898657-3e91760cbb34?auto=format&fit=crop&w=800&q=80",
    "france": "https://images.unsplash.com/photo-1502602898657-3e91760cbb34?auto=format&fit=crop&w=800&q=80",
    "london": "https://images.unsplash.com/photo-1513635269975-59663e0ac1ad?auto=format&fit=crop&w=800&q=80",
    "uk": "https://images.unsplash.com/photo-1513635269975-59663e0ac1ad?auto=format&fit=crop&w=800&q=80",
    "dubai": "https://images.unsplash.com/photo-1512453979798-5ea266f8880c?auto=format&fit=crop&w=800&q=80",
    "bali": "https://images.unsplash.com/photo-1537996194471-e657df975ab4?auto=format&fit=crop&w=800&q=80",
    "thailand": "https://images.unsplash.com/photo-1528181304800-259b08848526?auto=format&fit=crop&w=800&q=80",
    "singapore": "https://images.unsplash.com/photo-1525625293386-3f8f99389edd?auto=format&fit=crop&w=800&q=80",
    "switzerland": "https://images.unsplash.com/photo-1530122037265-a5f1f91d3b99?auto=format&fit=crop&w=800&q=80",
    "italy": "https://images.unsplash.com/photo-1516483638261-f4dbaf036963?auto=format&fit=crop&w=800&q=80",
    "rome": "https://images.unsplash.com/photo-1552832230-c0197dd311b5?auto=format&fit=crop&w=800&q=80",
    "new york": "https://images.unsplash.com/photo-1496442226666-8d4d0e62e6e9?auto=format&fit=crop&w=800&q=80",
}

DEFAULT_TRAVEL_COVER = "https://images.unsplash.com/photo-1488646953014-85cb44e25828?auto=format&fit=crop&w=800&q=80"


def resolve_destination_cover_image(destination_text: str | None) -> str:
    """Resolve an optimized cover image URL for a given destination name."""
    if not destination_text:
        return DEFAULT_TRAVEL_COVER

    normalized = destination_text.lower()
    for key, url in _CURATED_DESTINATION_COVERS.items():
        if key in normalized:
            return url

    return DEFAULT_TRAVEL_COVER
