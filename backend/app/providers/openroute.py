"""OpenRouteService geocoding, POI, and directions adapter."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from app.models.evidence import (
    FactStatus,
    GeoPoint,
    PlaceEvidence,
    RouteEvidence,
    SourceRef,
)
from app.models.context import (
    PlanningContext,
    ResolvedDestination,
    ResolvedStop,
)
from app.models.route import RouteMatrix, RouteMatrixLocation
from app.domain.destination import fallback_resolve_destination
from app.providers.http import ProviderHttpClient


class OpenRouteServiceProvider:
    BASE_URL = "https://api.openrouteservice.org"
    GEOCODE_URL = "https://api.openrouteservice.org/geocode/search"
    COUNTRY_ALPHA3 = {"IN": "IND", "JP": "JPN"}
    POI_CATEGORY_GROUPS = [130, 220, 330, 560, 620]
    INTEREST_SYNONYMS = {
        "architecture": {
            "historic",
            "monument",
            "palace",
            "mahal",
            "fort",
            "museum",
            "temple",
        },
        "beaches": {"beach", "coast", "natural"},
        "culture": {"arts", "culture", "historic", "museum", "temple", "tourism"},
        "food": {"restaurant", "food", "market", "cafe", "sustenance"},
        "gardens": {"garden", "park", "natural"},
        "heritage": {
            "historic",
            "monument",
            "palace",
            "mahal",
            "fort",
            "museum",
            "temple",
        },
        "local food": {"restaurant", "food", "market", "cafe", "sustenance"},
        "market": {"market", "bazaar", "shops"},
        "markets": {"market", "bazaar", "shops"},
        "nature": {"natural", "park", "garden", "viewpoint"},
        "temples": {"temple", "place of worship", "religion", "historic"},
        "technology": {"science", "technology", "museum"},
    }

    def __init__(self, api_key: str, timeout_seconds: float = 20) -> None:
        self._api_key = api_key
        self._http = ProviderHttpClient(timeout_seconds)
        self._geocode_cache: dict[str, GeoPoint] = {}
        self._route_cache: dict[tuple[str, str, str], RouteEvidence] = {}
        self._matrix_cache: dict[tuple[tuple[str, ...], str], RouteMatrix] = {}
        self._dynamic_sources: dict[str, SourceRef] = {}

    async def resolve(self, context: PlanningContext) -> ResolvedDestination:
        destination = fallback_resolve_destination(context)
        resolvable: list[ResolvedStop] = []
        unresolved: list[str] = []
        for stop in destination.stops[:8]:
            try:
                point = await self.geocode(
                    stop.geocode_query or stop.name,
                    country_code=stop.country_code,
                )
                self._geocode_cache[_geocode_cache_key(stop.name)] = point
                resolvable.append(
                    stop.model_copy(
                        update={
                            "latitude": point.latitude,
                            "longitude": point.longitude,
                        }
                    )
                )
            except Exception:
                unresolved.append(stop.name)
                resolvable.append(stop)
        assumptions = list(destination.assumptions)
        if unresolved:
            assumptions.append(
                "Could not live-geocode these confirmed stops; names were preserved: "
                + ", ".join(unresolved)
            )
        return destination.model_copy(
            update={
                "stops": resolvable,
                "assumptions": assumptions,
                "source_ids": ["src-ors-geocoding"],
            }
        )

    async def geocode(
        self,
        name: str,
        country_code: str | None = None,
        *,
        allow_regions: bool = False,
    ) -> GeoPoint:
        key = _geocode_cache_key(name, country_code, allow_regions)
        if key in self._geocode_cache:
            return self._geocode_cache[key]
        params: dict[str, Any] = {"text": name, "size": 5}
        alpha3 = self.COUNTRY_ALPHA3.get((country_code or "").upper())
        if alpha3:
            params["boundary.country"] = alpha3
        response = await self._http.get(
            self.GEOCODE_URL,
            params=params,
            headers={"Authorization": self._api_key},
        )
        features = response.json().get("features") or []
        if not features:
            raise LookupError(f"OpenRouteService could not geocode {name!r}")
        feature = _best_geocode_feature(
            features,
            alpha3,
            name,
            allow_regions=allow_regions,
        )
        longitude, latitude = feature["geometry"]["coordinates"]
        point = GeoPoint(latitude=latitude, longitude=longitude)
        self._geocode_cache[key] = point
        return point

    async def search_places(
        self,
        stop: ResolvedStop,
        context: PlanningContext,
    ) -> list[PlaceEvidence]:
        center = await self.geocode(
            stop.geocode_query or stop.name,
            country_code=stop.country_code,
        )
        self._geocode_cache[_geocode_cache_key(stop.name)] = center
        try:
            response = await self._http.post(
                f"{self.BASE_URL}/pois",
                headers={
                    "Authorization": self._api_key,
                    "Content-Type": "application/json",
                },
                json={
                    "request": "pois",
                    "geometry": {
                        "geojson": {
                            "type": "Point",
                            "coordinates": [center.longitude, center.latitude],
                        },
                        "buffer": _poi_search_buffer(stop, context),
                    },
                    "filters": {"category_group_ids": self.POI_CATEGORY_GROUPS},
                    "limit": 100,
                    "sortby": "distance",
                },
            )
        except Exception:
            return await self._search_wikipedia_places(stop, context, center)
        places: list[PlaceEvidence] = []
        for index, feature in enumerate(response.json().get("features") or []):
            properties: dict[str, Any] = feature.get("properties") or {}
            tags: dict[str, Any] = properties.get("osm_tags") or {}
            name = tags.get("name")
            coordinates = (feature.get("geometry") or {}).get("coordinates") or []
            if not name or len(coordinates) < 2:
                continue
            category_map = properties.get("category_ids") or {}
            categories = [
                str(details.get("category_name"))
                for details in category_map.values()
                if isinstance(details, dict) and details.get("category_name")
            ]
            provider_id = (
                f"{properties.get('osm_type', 'osm')}-{properties.get('osm_id', index)}"
            )
            places.append(
                PlaceEvidence(
                    id=f"ors-place-{provider_id}",
                    name=str(name),
                    stop_name=stop.name,
                    categories=categories,
                    address=_address_from_tags(tags),
                    location=GeoPoint(
                        latitude=float(coordinates[1]),
                        longitude=float(coordinates[0]),
                    ),
                    estimated_visit_minutes=90,
                    opening_hours=tags.get("opening_hours"),
                    status=FactStatus.VERIFIED,
                    source_ids=["src-ors-poi"],
                )
            )
        if not places:
            return await self._search_wikipedia_places(stop, context, center)
        return _rank_places(places, context)[:12]

    async def _search_wikipedia_places(
        self,
        stop: ResolvedStop,
        context: PlanningContext,
        center: GeoPoint,
    ) -> list[PlaceEvidence]:
        response = await self._http.get(
            "https://en.wikipedia.org/w/api.php",
            headers={"Api-User-Agent": "RiyaTripPlanner-PlaceDiscovery/0.1"},
            params={
                "action": "query",
                "list": "geosearch",
                "gscoord": f"{center.latitude}|{center.longitude}",
                "gsradius": 10_000,
                "gslimit": 50,
                "gsnamespace": 0,
                "format": "json",
            },
        )
        results: list[dict[str, Any]] = (response.json().get("query") or {}).get(
            "geosearch"
        ) or []
        places: list[PlaceEvidence] = []
        retrieved_at = datetime.now(UTC)
        for result in results:
            page_id = result.get("pageid")
            title = str(result.get("title") or "").strip()
            latitude = result.get("lat")
            longitude = result.get("lon")
            if page_id is None or not title or latitude is None or longitude is None:
                continue
            source_id = f"src-wikipedia-{page_id}"
            self._dynamic_sources[source_id] = SourceRef(
                id=source_id,
                provider="wikipedia",
                kind="place",
                title=f"Wikipedia: {title}",
                url=f"https://en.wikipedia.org/?curid={page_id}",
                provider_record_id=str(page_id),
                retrieved_at=retrieved_at,
            )
            places.append(
                PlaceEvidence(
                    id=f"wikipedia-place-{page_id}",
                    name=title,
                    stop_name=stop.name,
                    categories=_wikipedia_title_categories(title),
                    address=stop.name,
                    location=GeoPoint(
                        latitude=float(latitude),
                        longitude=float(longitude),
                    ),
                    estimated_visit_minutes=90,
                    status=FactStatus.VERIFIED,
                    source_ids=[source_id],
                )
            )
        ranked_places = _rank_places(places, context)[:12]
        thumbnails = await self._wikipedia_thumbnails(
            [place.id.removeprefix("wikipedia-place-") for place in ranked_places]
        )
        return [
            place.model_copy(
                update={
                    "image_url": thumbnails.get(
                        place.id.removeprefix("wikipedia-place-")
                    )
                }
            )
            for place in ranked_places
        ]

    async def _wikipedia_thumbnails(self, page_ids: list[str]) -> dict[str, str]:
        if not page_ids:
            return {}
        try:
            response = await self._http.get(
                "https://en.wikipedia.org/w/api.php",
                headers={"Api-User-Agent": "RiyaTripPlanner-PlaceDiscovery/0.1"},
                params={
                    "action": "query",
                    "pageids": "|".join(page_ids),
                    "prop": "pageimages",
                    "piprop": "thumbnail",
                    "pithumbsize": 960,
                    "format": "json",
                },
            )
        except Exception:
            return {}
        pages: dict[str, Any] = (response.json().get("query") or {}).get("pages") or {}
        thumbnails: dict[str, str] = {}
        for page_id, page in pages.items():
            thumbnail = page.get("thumbnail") if isinstance(page, dict) else None
            source = thumbnail.get("source") if isinstance(thumbnail, dict) else None
            if isinstance(source, str) and source.startswith(("https://", "http://")):
                thumbnails[str(page_id)] = source
        return thumbnails

    async def get_route(
        self,
        origin_name: str,
        destination_name: str,
        mode: str,
        *,
        origin_location: GeoPoint | None = None,
        destination_location: GeoPoint | None = None,
    ) -> RouteEvidence:
        cache_key = (
            origin_name.casefold(),
            destination_name.casefold(),
            _ors_profile(mode) or mode.casefold(),
        )
        if cache_key in self._route_cache:
            return self._route_cache[cache_key]
        origin = origin_location or await self.geocode(origin_name)
        destination = destination_location or await self.geocode(destination_name)
        profile = _ors_profile(mode)
        if profile is None:
            result = _estimated_non_road_route(
                origin_name,
                destination_name,
                mode,
                origin,
                destination,
            )
            self._route_cache[cache_key] = result
            return result
        response = await self._http.post(
            f"{self.BASE_URL}/v2/directions/{profile}",
            headers={
                "Authorization": self._api_key,
                "Content-Type": "application/json",
            },
            json={
                "coordinates": [
                    [origin.longitude, origin.latitude],
                    [destination.longitude, destination.latitude],
                ]
            },
        )
        routes = response.json().get("routes") or []
        if not routes:
            raise LookupError(f"No route from {origin_name} to {destination_name}")
        summary = routes[0].get("summary") or {}
        slug = f"{_slug(origin_name)}-{_slug(destination_name)}"
        result = RouteEvidence(
            id=f"ors-route-{slug}",
            origin_name=origin_name,
            destination_name=destination_name,
            origin_location=origin,
            destination_location=destination,
            mode=_display_mode(mode),
            distance_km=round(float(summary.get("distance", 0)) / 1000, 1),
            duration_minutes=max(1, round(float(summary.get("duration", 0)) / 60)),
            status=FactStatus.VERIFIED,
            source_ids=["src-ors-directions"],
        )
        self._route_cache[cache_key] = result
        return result

    async def get_route_matrix(
        self,
        locations: list[RouteMatrixLocation],
        mode: str,
    ) -> RouteMatrix:
        """Return one road-time matrix for global stop-order optimization."""

        profile = _ors_profile(mode)
        if profile is None:
            raise ValueError(f"OpenRouteService has no matrix profile for {mode!r}")
        resolved = [
            item.location or await self.geocode(item.label) for item in locations
        ]
        cache_key = (
            tuple(f"{point.latitude:.6f},{point.longitude:.6f}" for point in resolved),
            profile,
        )
        if cache_key in self._matrix_cache:
            return self._matrix_cache[cache_key]
        response = await self._http.post(
            f"{self.BASE_URL}/v2/matrix/{profile}",
            headers={
                "Authorization": self._api_key,
                "Content-Type": "application/json",
            },
            json={
                "locations": [[point.longitude, point.latitude] for point in resolved],
                "metrics": ["duration", "distance"],
                "units": "km",
            },
        )
        payload = response.json()
        raw_durations = payload.get("durations") or []
        raw_distances = payload.get("distances") or []
        size = len(locations)
        if len(raw_durations) != size or len(raw_distances) != size:
            raise LookupError("OpenRouteService returned an incomplete route matrix")
        matrix = RouteMatrix(
            labels=[item.label for item in locations],
            durations_minutes=[
                [
                    None if value is None else max(0, round(float(value) / 60))
                    for value in row
                ]
                for row in raw_durations
            ],
            distances_km=[
                [None if value is None else round(float(value), 1) for value in row]
                for row in raw_distances
            ],
            source_ids=["src-ors-matrix"],
        )
        self._matrix_cache[cache_key] = matrix
        return matrix

    def sources(self) -> list[SourceRef]:
        now = datetime.now(UTC)
        static_sources = [
            SourceRef(
                id="src-ors-geocoding",
                provider="openrouteservice",
                kind="geocoding",
                title="OpenRouteService geocoding",
                url="https://openrouteservice.org/dev/",
                retrieved_at=now,
            ),
            SourceRef(
                id="src-ors-poi",
                provider="openrouteservice",
                kind="place",
                title="OpenRouteService POI data from OpenStreetMap",
                url="https://giscience.github.io/openrouteservice/api-reference/endpoints/poi/",
                retrieved_at=now,
            ),
            SourceRef(
                id="src-ors-directions",
                provider="openrouteservice",
                kind="route",
                title="OpenRouteService directions",
                url="https://giscience.github.io/openrouteservice/api-reference/endpoints/directions/",
                retrieved_at=now,
            ),
            SourceRef(
                id="src-ors-matrix",
                provider="openrouteservice",
                kind="route-matrix",
                title="OpenRouteService travel-time matrix",
                url="https://giscience.github.io/openrouteservice/api-reference/endpoints/matrix/",
                retrieved_at=now,
            ),
            SourceRef(
                id="src-estimated-transport",
                provider="trip-planner",
                kind="route-estimate",
                title="Mode-aware non-road transport estimate",
                retrieved_at=now,
            ),
        ]
        return [*static_sources, *self._dynamic_sources.values()]

    async def close(self) -> None:
        await self._http.close()


def _rank_places(
    places: list[PlaceEvidence],
    context: PlanningContext,
) -> list[PlaceEvidence]:
    wanted = {value.casefold() for value in context.activity_interests}
    must_see = {value.casefold() for value in context.must_see}
    avoided = {value.casefold() for value in context.avoid}
    order = {place.id: index for index, place in enumerate(places)}

    def score(place: PlaceEvidence) -> tuple[int, int, int, int]:
        haystack = " ".join([place.name, *place.categories]).casefold()
        required_score = 100 * sum(term in haystack for term in must_see)
        interest_terms = {
            synonym
            for interest in wanted
            for synonym in OpenRouteServiceProvider.INTEREST_SYNONYMS.get(
                interest,
                {interest},
            )
        }
        interest_score = 10 * sum(term in haystack for term in interest_terms)
        landmark_score = 2 * sum(
            term in haystack
            for term in (
                "historic",
                "monument",
                "museum",
                "palace",
                "mahal",
                "fort",
                "temple",
                "tourism",
                "viewpoint",
            )
        )
        avoid_penalty = -100 * sum(term in haystack for term in avoided)
        return (
            required_score + interest_score + landmark_score + avoid_penalty,
            required_score,
            landmark_score,
            -order[place.id],
        )

    return sorted(places, key=score, reverse=True)


def _poi_search_buffer(stop: ResolvedStop, context: PlanningContext) -> int:
    normalized = stop.name.casefold()
    if "goa" in normalized:
        return 40_000
    if normalized in {"srinagar", "gulmarg", "pahalgam"}:
        return 20_000
    if context.destination.type.casefold() in {"city", "cities"}:
        return 15_000
    return 8_000


def _wikipedia_title_categories(title: str) -> list[str]:
    normalized = title.casefold()
    categories = ["notable place", "wikipedia"]
    keyword_categories = {
        "beach": "beach",
        "bazaar": "market",
        "fort": "historic",
        "garden": "garden",
        "mahal": "palace",
        "market": "market",
        "museum": "museum",
        "palace": "palace",
        "park": "natural",
        "temple": "temple",
    }
    categories.extend(
        category
        for keyword, category in keyword_categories.items()
        if keyword in normalized
    )
    return categories


def _address_from_tags(tags: dict[str, Any]) -> str | None:
    parts = [
        tags.get("addr:housenumber"),
        tags.get("addr:street"),
        tags.get("addr:city"),
    ]
    address = ", ".join(str(part) for part in parts if part)
    return address or None


def _geocode_cache_key(
    name: str,
    country_code: str | None = None,
    allow_regions: bool = False,
) -> str:
    return f"{name.casefold()}|{(country_code or '').upper()}|regions={allow_regions}"


def _ors_profile(mode: str) -> str | None:
    return {
        "car": "driving-car",
        "taxi": "driving-car",
        "driving": "driving-car",
        "self_driving": "driving-car",
        "self-driving": "driving-car",
        "scooter": "driving-car",
        "walk": "foot-walking",
        "walking": "foot-walking",
        "bicycle": "cycling-regular",
        "cycling": "cycling-regular",
    }.get(mode.casefold())


def _display_mode(mode: str) -> str:
    normalized = mode.casefold()
    return {
        "plane": "flight",
        "air": "flight",
        "rail": "train",
        "walk": "walking",
        "driving": "car",
        "self_driving": "car",
        "self-driving": "car",
    }.get(normalized, normalized)


def _best_geocode_feature(
    features: list[dict[str, Any]],
    expected_country: str | None,
    requested_name: str | None = None,
    *,
    allow_regions: bool = False,
) -> dict[str, Any]:
    country_matches = []
    for feature in features:
        properties = feature.get("properties") or {}
        country = str(properties.get("country_a") or "").upper()
        if not expected_country or not country or country == expected_country:
            country_matches.append(feature)

    if not country_matches:
        raise LookupError("OpenRouteService returned no result in the expected country")
    candidates = country_matches
    if requested_name:
        for feature in candidates:
            if _is_usable_geocode_feature(
                feature,
                requested_name,
                allow_regions=allow_regions,
            ):
                return feature
        raise LookupError(
            f"OpenRouteService returned no specific match for {requested_name!r}"
        )
    return candidates[0]


def _is_usable_geocode_feature(
    feature: dict[str, Any],
    requested_name: str,
    *,
    allow_regions: bool = False,
) -> bool:
    """Reject broad region centroids masquerading as a requested town.

    Pelias can return a country or state centroid when it cannot understand a
    detailed village query.  Those coordinates are valid map points, but they
    are not valid coordinates for the requested stop.
    """

    properties = feature.get("properties") or {}
    if not properties:
        # Some compatible geocoders and test doubles return geometry only.
        return True

    layer = str(properties.get("layer") or "").casefold()
    feature_name = str(properties.get("name") or "").casefold().strip()
    requested_label = requested_name.split(",", 1)[0].casefold().strip()

    # Do not turn an arbitrary town request into a country centroid. A matching
    # regional centroid, however, is a valid requested destination (Goa, Bali,
    # Kerala, and similar region-level trips).
    if not allow_regions and layer == "country":
        return False
    if (
        not allow_regions
        and layer in {"macroregion", "region"}
        and requested_label != feature_name
    ):
        return False

    requested_tokens = {
        token for token in requested_label.replace("-", " ").split() if len(token) >= 3
    }
    result_text = " ".join(
        str(properties.get(field) or "")
        for field in (
            "name",
            "label",
            "locality",
            "localadmin",
            "county",
            "region",
        )
    ).casefold()
    name_matches = bool(requested_tokens) and any(
        token in result_text for token in requested_tokens
    )
    confidence = float(properties.get("confidence") or 0)
    return name_matches or confidence >= 0.8


def _estimated_non_road_route(
    origin_name: str,
    destination_name: str,
    mode: str,
    origin: GeoPoint,
    destination: GeoPoint,
) -> RouteEvidence:
    normalized = mode.casefold()
    direct_distance = _haversine_km(origin, destination)
    if normalized in {"flight", "plane", "air"}:
        distance = direct_distance * 1.05
        duration = round((distance / 850) * 60 + 150)
        normalized = "flight"
    elif normalized in {"train", "rail", "metro"}:
        distance = direct_distance * 1.08
        speed = 155 if normalized != "metro" else 35
        duration = round((distance / speed) * 60 + 30)
        normalized = "train" if normalized != "metro" else "metro"
    else:
        distance = direct_distance * 1.15
        duration = round((distance / 35) * 60 + 20)
    slug = f"{_slug(origin_name)}-{_slug(destination_name)}"
    return RouteEvidence(
        id=f"estimated-route-{slug}",
        origin_name=origin_name,
        destination_name=destination_name,
        origin_location=origin,
        destination_location=destination,
        mode=normalized,
        distance_km=round(distance, 1),
        duration_minutes=max(15, duration),
        status=FactStatus.ESTIMATED,
        source_ids=["src-estimated-transport"],
    )


def _haversine_km(origin: GeoPoint, destination: GeoPoint) -> float:
    radius_km = 6371.0
    lat1 = math.radians(origin.latitude)
    lat2 = math.radians(destination.latitude)
    delta_lat = math.radians(destination.latitude - origin.latitude)
    delta_lon = math.radians(destination.longitude - origin.longitude)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return radius_km * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def _slug(value: str) -> str:
    return "-".join(value.casefold().replace(",", " ").split())
