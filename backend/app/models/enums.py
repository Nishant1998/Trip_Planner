"""Shared enums used across trip collection, storage, and planning flow."""

from __future__ import annotations

from enum import StrEnum


class LiveStatusPhase(StrEnum):
    """Frontend-facing live status labels for collection progress."""

    BOOTSTRAPPING = "bootstrapping"
    CLASSIFYING = "classifying"
    EXTRACTING = "extracting"
    VALIDATING = "validating"
    CLARIFYING = "clarifying"
    ENRICHING = "enriching"
    FINAL_CHECKS = "final_checks"
    PREPARING_REPLY = "preparing_reply"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PLANNING = "planning"
    IDLE = "idle"
    ERROR = "error"


class TripStatus(StrEnum):
    """High-level lifecycle states for one trip."""

    DRAFT = "draft"
    COLLECTING = "collecting"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PLANNING = "planning"
    CHECKPOINT_REVIEW = "checkpoint_review"
    DRAFT_REVIEW = "draft_review"
    CONFIRMED = "confirmed"
    ARCHIVED = "archived"
    CANCELLED = "cancelled"
    FAILED = "failed"


class DestType(StrEnum):
    """Supported destination input types."""

    CITY = "city"
    CITIES = "cities"
    REGION = "region"
    ROUTE = "route"
    POI = "poi"
    OPEN = "open"


class TripScope(StrEnum):
    """Top-level trip geography categories."""

    DOMESTIC = "domestic"
    INTERNATIONAL = "international"


class TravelStyle(StrEnum):
    """Preferred day pace for the trip."""

    RELAXED = "relaxed"
    MODERATE = "moderate"
    PACKED = "packed"


class SpendingTier(StrEnum):
    """Budget comfort bands used in prompts and checks."""

    BUDGET = "budget"
    MID_RANGE = "mid_range"
    PREMIUM = "premium"
    LUXURY = "luxury"


class HotelTier(StrEnum):
    """Preferred hotel comfort bands."""

    BUDGET = "budget"
    MID_RANGE = "mid_range"
    PREMIUM = "premium"
    LUXURY = "luxury"
    ANY = "any"


class TripPurpose(StrEnum):
    """Primary purpose labels for a trip."""

    LEISURE = "leisure"
    FAMILY_VISIT = "family_visit"
    PILGRIMAGE = "pilgrimage"
    BUSINESS = "business"
    EDUCATION = "education"
    MEDICAL = "medical"
    EVENT = "event"
    HONEYMOON = "honeymoon"
    SHOPPING = "shopping"
    ADVENTURE = "adventure"
    ROAD_TRIP = "road_trip"
    WEEKEND_TRIP = "weekend_trip"
    OTHER = "other"


class TimeWindow(StrEnum):
    """Parts of the day used for preference capture."""

    EARLY_MORNING = "early_morning"
    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"
    NIGHT = "night"
    ANY = "any"


class TransportLong(StrEnum):
    """Long-distance travel mode options."""

    FLIGHT = "flight"
    TRAIN = "train"
    BUS = "bus"
    SELF_DRIVING = "self_driving"
    ANY = "any"


class TransportLocal(StrEnum):
    """Local in-destination travel mode options."""

    WALKING = "walking"
    TAXI = "taxi"
    PUBLIC_TRANSPORT = "public_transport"
    SELF_DRIVING = "self_driving"
    ANY = "any"


class TrainClass(StrEnum):
    """Supported train class preferences."""

    SLEEPER = "sleeper"
    AC_3E = "ac_3e"
    AC_3 = "ac_3"
    AC_2 = "ac_2"
    AC_FIRST = "ac_first"
    CHAIR_CAR = "chair_car"
    EXEC_CHAIR = "exec_chair"
    SECOND_SITTING = "second_sitting"
    GENERAL = "general"
    ANY = "any"


class FlightCabin(StrEnum):
    """Supported flight cabin preferences."""

    ECONOMY = "economy"
    PREMIUM_ECONOMY = "premium_economy"
    BUSINESS = "business"
    FIRST = "first"
    ANY = "any"


class LodgingType(StrEnum):
    """Supported accommodation categories."""

    HOTEL = "hotel"
    HOMESTAY = "homestay"
    HOSTEL = "hostel"
    APARTMENT = "apartment"
    ANY = "any"


class RoomType(StrEnum):
    """Supported room-style preferences."""

    ENTIRE_PLACE = "entire_place"
    PRIVATE_ROOM = "private_room"
    SHARED_ROOM = "shared_room"
    ANY = "any"


class DataConfidence(StrEnum):
    """Confidence labels for externally gathered facts."""

    VERIFIED = "verified"
    PARTIALLY_VERIFIED = "partially_verified"
    APPROXIMATE = "approximate"


class FieldSource(StrEnum):
    """Where a collected field value came from."""

    USER = "user"
    PROFILE = "profile"
    INFERRED = "inferred"
    DEFAULT = "default"


class FieldConfidence(StrEnum):
    """How strongly a field value should be trusted."""

    HARD = "hard"
    SOFT = "soft"


class PlanningArtifactType(StrEnum):
    """Artifact buckets created during planning and checkpointing."""

    RETRIEVAL = "retrieval"
    RERANKING = "reranking"
    SKELETON = "skeleton"
    DETAIL_FILL = "detail_fill"
    GOVERNANCE = "governance"
    CHECKPOINT = "checkpoint"
