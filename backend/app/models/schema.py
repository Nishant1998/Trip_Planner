"""Static trip-memory field schema used to build agent-facing views."""

from __future__ import annotations

from app.models.enums import (
    DestType,
    FlightCabin,
    HotelTier,
    LodgingType,
    RoomType,
    SpendingTier,
    TimeWindow,
    TrainClass,
    TransportLocal,
    TransportLong,
    TravelStyle,
    TripPurpose,
    TripScope,
    TripStatus,
)

TRIP_FIELD_SCHEMA: dict[str, dict[str, dict]] = {
    "metadata": {
        "slug": {
            "type": "string",
            "default": None,
            "options": None,
            "desc": "Unique URL-safe trip identifier.",
        },
        "trip_name": {
            "type": "string",
            "default": None,
            "options": None,
            "desc": "Human-readable trip title shown in the UI.",
        },
        "trip_description": {
            "type": "string",
            "default": None,
            "options": None,
            "desc": "Short summary describing the trip purpose or theme.",
        },
        "status": {
            "type": "enum",
            "default": TripStatus.DRAFT.value,
            "options": [status.value for status in TripStatus],
            "desc": "Current trip lifecycle stage.",
        },
        "currency_code": {
            "type": "string",
            "default": None,
            "options": None,
            "desc": "Three-letter currency for the traveller's total budget and cost estimates.",
        },
        "timezone": {
            "type": "string",
            "default": None,
            "options": None,
            "desc": "Primary IANA timezone used for local scheduling at the destination.",
        },
        "trip_scope": {
            "type": "enum",
            "default": None,
            "options": [scope.value for scope in TripScope],
            "desc": "Whether the trip is domestic or international.",
        },
    },
    "needed": {
        "origin": {
            "type": "string",
            "options": None,
            "desc": "City or location where the journey starts.",
        },
        "destination": {
            "type": "object",
            "options": {
                "destination_types": [item.value for item in DestType],
                # Exposes the destination subtype key to the extraction agent so pilgrimage circuits and route semantics are correctly structured.
                "object_keys": ["type", "subtype", "items"],
            },
            "desc": "Resolved destination object with type, optional subtype, and ordered places.",
        },
        "start_date": {
            "type": "date",
            "options": None,
            "desc": "Trip start date in YYYY-MM-DD format.",
        },
        "end_date": {
            "type": "date",
            "options": None,
            "desc": "Trip end date in YYYY-MM-DD format.",
        },
        "trip_duration_days": {
            "type": "int",
            "options": None,
            "desc": "Total trip length in days.",
        },
        "num_adults": {
            "type": "int",
            "options": None,
            "desc": "Number of adult travelers.",
        },
        "trip_budget": {
            "type": "money",
            "options": None,
            "desc": "Total trip budget in the trip currency.",
        },
    },
    "optional": {
        "end_location": {
            "type": "string",
            "default": "origin",
            "options": None,
            "desc": "Where the trip ends. Defaults to the origin city.",
        },
        "trip_purpose": {
            "type": "enum",
            "default": TripPurpose.LEISURE.value,
            "options": [purpose.value for purpose in TripPurpose],
            "desc": "Primary purpose of the trip.",
        },
        "num_children": {
            "type": "int",
            "default": 0,
            "options": None,
            "desc": "Number of children traveling.",
        },
        "children_ages": {
            "type": "int[]",
            "default": [],
            "options": None,
            "desc": "Ages of children traveling.",
        },
        "num_seniors": {
            "type": "int",
            "default": 0,
            "options": None,
            "desc": "Number of senior travelers.",
        },
        "depart_time_window": {
            "type": "enum",
            "default": TimeWindow.ANY.value,
            "options": [window.value for window in TimeWindow],
            "desc": "Preferred outbound departure time window.",
        },
        "arrival_time_window": {
            "type": "enum",
            "default": TimeWindow.ANY.value,
            "options": [window.value for window in TimeWindow],
            "desc": "Preferred arrival time window at the destination.",
        },
        "return_time_window": {
            "type": "enum",
            "default": TimeWindow.ANY.value,
            "options": [window.value for window in TimeWindow],
            "desc": "Preferred return departure time window.",
        },
        "day_start_time": {
            "type": "time",
            "default": "09:00",
            "options": None,
            "desc": "Preferred daily activity start time in HH:MM format.",
        },
        "day_end_time": {
            "type": "time",
            "default": "21:00",
            "options": None,
            "desc": "Preferred daily activity end time in HH:MM format.",
        },
        "travel_style": {
            "type": "enum",
            "default": TravelStyle.MODERATE.value,
            "options": [style.value for style in TravelStyle],
            "desc": "Preferred travel pace.",
        },
        "spending_tier": {
            "type": "enum",
            "default": SpendingTier.MID_RANGE.value,
            "options": [tier.value for tier in SpendingTier],
            "desc": "General spending comfort level.",
        },
        "local_transport_modes": {
            "type": "enum[]",
            "default": [TransportLocal.ANY.value],
            "options": [mode.value for mode in TransportLocal],
            "desc": "Preferred transportation modes within the destination.",
        },
        "long_distance_modes": {
            "type": "enum[]",
            "default": [TransportLong.ANY.value],
            "options": [mode.value for mode in TransportLong],
            "desc": "Preferred transportation modes between cities.",
        },
        "train_class": {
            "type": "enum",
            "default": TrainClass.ANY.value,
            "options": [item.value for item in TrainClass],
            "desc": "Preferred train travel class.",
        },
        "flight_cabin_class": {
            "type": "enum",
            "default": FlightCabin.ANY.value,
            "options": [item.value for item in FlightCabin],
            "desc": "Preferred flight cabin class.",
        },
        "activity_interests": {
            "type": "string[]",
            "default": None,
            "options": None,
            "desc": "Activities or experiences the traveler enjoys.",
        },
        "must_see": {
            "type": "string[]",
            "default": None,
            "options": None,
            "desc": "Places or experiences that must be included.",
        },
        "avoid": {
            "type": "string[]",
            "default": None,
            "options": None,
            "desc": "Places or experiences the traveler wants to avoid.",
        },
        "cuisine_prefs": {
            "type": "string[]",
            "default": None,
            "options": None,
            "desc": "Preferred cuisines or food styles.",
        },
        "dietary_restrictions": {
            "type": "string[]",
            "default": None,
            "options": None,
            "desc": "Dietary requirements or restrictions.",
        },
        "accessibility_needs": {
            "type": "string[]",
            "default": None,
            "options": None,
            "desc": "Accessibility or mobility requirements.",
        },
        "hotel_tier": {
            "type": "enum",
            "default": HotelTier.MID_RANGE.value,
            "options": [item.value for item in HotelTier],
            "desc": "Preferred accommodation comfort level.",
        },
        "lodging_type": {
            "type": "enum",
            "default": LodgingType.ANY.value,
            "options": [item.value for item in LodgingType],
            "desc": "Preferred accommodation type.",
        },
        "room_type_pref": {
            "type": "enum",
            "default": RoomType.ANY.value,
            "options": [item.value for item in RoomType],
            "desc": "Preferred room privacy level.",
        },
        "trip_notes": {
            "type": "string[]",
            "default": None,
            "options": None,
            "desc": "Free-text notes, conditional preferences, or soft observations.",
        },
    },
}
