"""Google Maps links assembled from known places and coordinates."""

from __future__ import annotations

from urllib.parse import urlencode

from .contracts import Coordinates, RoutePoint


def place_map_url(name: str, place_id: str | None = None,
                  location: Coordinates | None = None) -> str:
    query = name or (f"{location.latitude},{location.longitude}" if location else "")
    if not query:
        raise ValueError("A place name or coordinates are required")
    params = {"api": "1", "query": query}
    if place_id:
        params["query_place_id"] = place_id
    return "https://www.google.com/maps/search/?" + urlencode(params)


def route_map_url(origin: RoutePoint, destination: RoutePoint,
                  mode: str = "DRIVE") -> str:
    """Open a selected route in Google Maps without a Routes API request."""
    travel_modes = {"DRIVE": "driving", "WALK": "walking",
                    "BICYCLE": "bicycling", "TRANSIT": "transit"}
    if mode not in travel_modes:
        raise ValueError("unsupported Google Maps travel mode")
    def coords(point: RoutePoint) -> str:
        return f"{point.location.latitude},{point.location.longitude}"
    return "https://www.google.com/maps/dir/?" + urlencode({
        "api": "1", "origin": coords(origin), "destination": coords(destination),
        "travelmode": travel_modes[mode],
    })
