"""General transport classification and user-mode policy.

This module intentionally contains no provider HTTP calls and no trip-specific
knowledge.  It answers two separate questions: what movement is occurring, and
which user-allowed mode can a provider be asked to calculate.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..models.planning import (
    ModeResolution,
    ModeSelectionStatus,
    MovementClass,
    TransportMode,
)

MODE_ALIASES = {
    "walk": TransportMode.WALK,
    "walking": TransportMode.WALK,
    "car": TransportMode.DRIVE,
    "drive": TransportMode.DRIVE,
    "driving": TransportMode.DRIVE,
    "private_car": TransportMode.DRIVE,
    "private car": TransportMode.DRIVE,
    "self_driving": TransportMode.DRIVE,
    "self driving": TransportMode.DRIVE,
    "road": TransportMode.DRIVE,
    "taxi": TransportMode.TAXI,
    "cab": TransportMode.TAXI,
    "rental_car": TransportMode.RENTAL_CAR,
    "rental car": TransportMode.RENTAL_CAR,
    "transit": TransportMode.TRANSIT,
    "public_transport": TransportMode.TRANSIT,
    "public transport": TransportMode.TRANSIT,
    "public_transit": TransportMode.TRANSIT,
    "public transit": TransportMode.TRANSIT,
    "metro": TransportMode.METRO,
    "subway": TransportMode.METRO,
    "bus": TransportMode.BUS,
    "train": TransportMode.TRAIN,
    "rail": TransportMode.TRAIN,
    "flight": TransportMode.FLIGHT,
    "plane": TransportMode.FLIGHT,
    "bike": TransportMode.CYCLING,
    "bicycle": TransportMode.CYCLING,
    "cycling": TransportMode.CYCLING,
}

TERMINAL_TYPES = frozenset({"airport", "international_airport", "train_station",
                            "bus_station", "transit_station", "ferry_terminal"})


def is_terminal_stop(stop) -> bool:
    return bool(TERMINAL_TYPES.intersection({
        getattr(stop, "type", None), getattr(stop, "provider_primary_type", None),
        *getattr(stop, "provider_types", []),
    }))

PROVIDER_MODE = {
    TransportMode.WALK: "WALK",
    TransportMode.DRIVE: "DRIVE",
    TransportMode.TAXI: "DRIVE",
    TransportMode.RENTAL_CAR: "DRIVE",
    TransportMode.CYCLING: "BICYCLE",
    TransportMode.TRANSIT: "TRANSIT",
    TransportMode.METRO: "TRANSIT",
    TransportMode.BUS: "TRANSIT",
    TransportMode.TRAIN: "TRANSIT",
}


@dataclass(frozen=True)
class TransportEndpoint:
    entity_id: str
    base_id: str | None = None
    region_id: str | None = None
    country_code: str | None = None
    role: str = "unknown"
    route_context: bool = False


def normalize_mode(value: object) -> TransportMode:
    """Safely normalize one user value without guessing from arbitrary text."""
    if not isinstance(value, str):
        return TransportMode.UNKNOWN
    normalized = value.strip().casefold().replace("-", "_")
    return MODE_ALIASES.get(normalized, TransportMode.UNKNOWN)


def normalize_modes(values: object) -> list[TransportMode]:
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        mode = normalize_mode(value)
        if mode not in result:
            result.append(mode)
    return result


def classify_movement(
    origin: TransportEndpoint,
    destination: TransportEndpoint,
    *,
    international_trip_boundary: bool = False,
) -> MovementClass:
    """Classify endpoints using only explicit structured relationships."""
    if (
        origin.country_code
        and destination.country_code
        and origin.country_code != destination.country_code
    ):
        return MovementClass.INTERNATIONAL
    if (
        international_trip_boundary
        and {origin.role, destination.role} & {"origin", "end"}
        and {origin.role, destination.role} & {"base"}
    ):
        return MovementClass.INTERNATIONAL
    if origin.entity_id == destination.entity_id:
        return MovementClass.LOCAL
    if origin.route_context and destination.route_context:
        return MovementClass.ROUTE_STAGE
    if origin.base_id and destination.base_id:
        return (
            MovementClass.LOCAL
            if origin.base_id == destination.base_id
            else MovementClass.INTER_BASE
        )
    if origin.region_id and destination.region_id:
        return (
            MovementClass.LOCAL
            if origin.region_id == destination.region_id
            else MovementClass.INTER_BASE
        )
    return MovementClass.UNKNOWN


def requested_modes(
    movement_class: MovementClass, preferences: dict
) -> list[TransportMode]:
    if movement_class == MovementClass.UNKNOWN:
        return []
    key = (
        "local_modes"
        if movement_class == MovementClass.LOCAL
        else "long_distance_modes"
    )
    values = preferences.get(key, [])
    if (
        movement_class == MovementClass.LOCAL
        and isinstance(values, list)
        and any(
            isinstance(value, str) and value.strip().casefold() == "any"
            for value in values
        )
    ):
        return [TransportMode.TRANSIT, TransportMode.WALK, TransportMode.DRIVE]
    return [mode for mode in normalize_modes(values) if mode != TransportMode.UNKNOWN]


def resolve_mode(
    movement_class: MovementClass,
    preferences: dict,
    supported_provider_modes: Iterable[str],
) -> ModeResolution:
    """Choose a provider request without ever expanding the user's allowed modes."""
    requested = requested_modes(movement_class, preferences)
    supported = {value.upper() for value in supported_provider_modes}
    if not requested:
        return ModeResolution(
            movement_class=movement_class,
            requested_modes=[],
            selection_status=ModeSelectionStatus.NO_ALLOWED_MODE,
            warnings=[
                "No usable transport preference was supplied for this movement class."
            ],
        )
    preferred = requested[0]
    if movement_class == MovementClass.INTERNATIONAL and preferred == TransportMode.FLIGHT:
        return ModeResolution(
            movement_class=movement_class, requested_modes=requested,
            preferred_mode=preferred,
            selection_status=ModeSelectionStatus.UNSUPPORTED,
            selected_mode_allowed_by_user=True, preference_satisfied=False,
            source="provider_capability",
            warnings=["Flight timing requires researched evidence or booking confirmation; ground transit is not a flight substitute."],
        )
    for index, mode in enumerate(requested):
        provider_mode = PROVIDER_MODE.get(mode)
        if provider_mode and provider_mode in supported:
            fallback = index > 0
            reason = (
                None
                if not fallback
                else f"Preferred {preferred.value} routing is unavailable; {mode.value} is another user-allowed mode."
            )
            return ModeResolution(
                movement_class=movement_class,
                requested_modes=requested,
                preferred_mode=preferred,
                selected_mode=mode,
                provider_mode=provider_mode,
                selection_status=(
                    ModeSelectionStatus.ALLOWED_ALTERNATIVE
                    if fallback
                    else ModeSelectionStatus.PREFERRED
                ),
                fallback_used=fallback,
                fallback_reason=reason,
                fallback_from=preferred if fallback else None,
                fallback_to=mode if fallback else None,
                selected_mode_allowed_by_user=True,
                preference_satisfied=not fallback,
                source="user_preferences",
                warnings=[reason] if reason else [],
            )
    return ModeResolution(
        movement_class=movement_class,
        requested_modes=requested,
        preferred_mode=preferred,
        selection_status=ModeSelectionStatus.UNSUPPORTED,
        preference_satisfied=False,
        source="provider_capability",
        warnings=[
            f"No configured provider supports the user-allowed {movement_class.value} modes."
        ],
    )
