"""Trip-memory models that hold the persisted canonical collection state."""

from __future__ import annotations

from datetime import date, time
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import (
    DataConfidence,
    DestType,
    FieldSource,
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

_LEGACY_FIELD_ALIASES = {
    "total_budget": "trip_budget",
}


def _canonical_variable_name(variable_name: str) -> str:
    """Return the canonical stored variable name for legacy-compatible inputs."""

    return _LEGACY_FIELD_ALIASES.get(variable_name, variable_name)


class FieldMeta(BaseModel):
    """Stored source metadata for one collected variable."""

    value: Any
    source: FieldSource


class Destination(BaseModel):
    """User-facing destination input with type and ordered items."""

    type: DestType
    items: list[str] = Field(default_factory=list)
    subtype: str | None = None


class TripMetadataValues(BaseModel):
    """Typed metadata values used for validation and normalization."""

    slug: str | None = None
    trip_name: str | None = None
    trip_description: str | None = None
    status: TripStatus = TripStatus.DRAFT
    currency_code: str | None = None
    timezone: str | None = None
    trip_scope: TripScope | None = None


class NeededFieldValues(BaseModel):
    """Typed required trip inputs used for validation and normalization."""

    origin: str | None = None
    destination: Destination | None = None
    start_date: date | None = None
    end_date: date | None = None
    trip_duration_days: int | None = None
    num_adults: int | None = None
    trip_budget: int | None = None


class OptionalFieldValues(BaseModel):
    """Typed optional trip inputs used for validation and normalization."""

    end_location: str | None = None
    trip_purpose: TripPurpose | None = None
    num_children: int | None = None
    children_ages: list[int] | None = None
    num_seniors: int | None = None
    depart_time_window: TimeWindow | None = None
    arrival_time_window: TimeWindow | None = None
    return_time_window: TimeWindow | None = None
    day_start_time: time | None = None
    day_end_time: time | None = None
    travel_style: TravelStyle | None = None
    spending_tier: SpendingTier | None = None
    local_transport_modes: list[TransportLocal] | None = None
    long_distance_modes: list[TransportLong] | None = None
    train_class: TrainClass | None = None
    flight_cabin_class: FlightCabin | None = None
    activity_interests: list[str] | None = None
    must_see: list[str] | None = None
    avoid: list[str] | None = None
    cuisine_prefs: list[str] | None = None
    dietary_restrictions: list[str] | None = None
    accessibility_needs: list[str] | None = None
    hotel_tier: HotelTier | None = None
    lodging_type: LodgingType | None = None
    room_type_pref: RoomType | None = None
    trip_notes: list[str] | None = None


class MetadataValueRecord(BaseModel):
    """One metadata variable stored in canonical DB shape."""

    value: Any = None


class SourcedValueRecord(BaseModel):
    """One sourced variable stored in canonical DB shape."""

    value: Any = None
    source: FieldSource | None = None


class _RecordSection(BaseModel):
    """Base helper for sections that store wrapped variable records."""

    model_config = ConfigDict(populate_by_name=True)
    VALUE_MODEL: ClassVar[type[BaseModel]]

    @classmethod
    def _alias_map(cls) -> dict[str, str]:
        return {field.alias or name: name for name, field in cls.model_fields.items()}

    @classmethod
    def has_variable(cls, variable_name: str) -> bool:
        return variable_name in cls._alias_map()

    @classmethod
    def variable_names(cls) -> list[str]:
        return list(cls._alias_map())

    def get_record(
        self, variable_name: str
    ) -> MetadataValueRecord | SourcedValueRecord:
        field_name = self.__class__._alias_map().get(variable_name)
        if field_name is None:
            raise AttributeError(f"Unknown variable: {variable_name}")
        return super().__getattribute__(field_name)

    def get_value(self, variable_name: str) -> Any:
        return self.get_record(variable_name).value

    def set_value(self, variable_name: str, value: Any) -> None:
        self.get_record(variable_name).value = value

    def to_value_dict(self) -> dict[str, Any]:
        return {
            variable_name: self.get_value(variable_name)
            for variable_name in self.__class__.variable_names()
        }

    @model_validator(mode="after")
    def _normalize_values(self) -> "_RecordSection":
        validated = self.VALUE_MODEL.model_validate(self.to_value_dict())
        for variable_name in self.__class__.variable_names():
            self.set_value(variable_name, getattr(validated, variable_name))
        return self

    def __getattr__(self, name: str) -> Any:
        field_name = self.__class__._alias_map().get(name)
        if field_name is None:
            raise AttributeError(name)
        return super().__getattribute__(field_name).value

    def __setattr__(self, name: str, value: Any) -> None:
        field_name = self.__class__._alias_map().get(name)
        if field_name is not None and name not in self.__class__.model_fields:
            super().__getattribute__(field_name).value = value
            return
        super().__setattr__(name, value)


class _SourcedRecordSection(_RecordSection):
    """Record section where every variable also stores a source."""

    def get_source(self, variable_name: str) -> FieldSource | None:
        return self.get_record(variable_name).source

    def set_source(self, variable_name: str, source: FieldSource | None) -> None:
        self.get_record(variable_name).source = source

    def iter_records(self) -> list[tuple[str, SourcedValueRecord]]:
        return [
            (variable_name, self.get_record(variable_name))
            for variable_name in self.__class__.variable_names()
        ]


class TripMetadata(_RecordSection):
    """Canonical metadata section stored as wrapped value records."""

    VALUE_MODEL: ClassVar[type[BaseModel]] = TripMetadataValues

    slug_record: MetadataValueRecord = Field(
        default_factory=MetadataValueRecord,
        alias="slug",
    )
    trip_name_record: MetadataValueRecord = Field(
        default_factory=MetadataValueRecord,
        alias="trip_name",
    )
    trip_description_record: MetadataValueRecord = Field(
        default_factory=MetadataValueRecord,
        alias="trip_description",
    )
    status_record: MetadataValueRecord = Field(
        default_factory=lambda: MetadataValueRecord(value=TripStatus.DRAFT),
        alias="status",
    )
    currency_code_record: MetadataValueRecord = Field(
        default_factory=MetadataValueRecord,
        alias="currency_code",
    )
    timezone_record: MetadataValueRecord = Field(
        default_factory=MetadataValueRecord,
        alias="timezone",
    )
    trip_scope_record: MetadataValueRecord = Field(
        default_factory=MetadataValueRecord,
        alias="trip_scope",
    )


class NeededFields(_SourcedRecordSection):
    """Canonical required section stored as wrapped value/source records."""

    VALUE_MODEL: ClassVar[type[BaseModel]] = NeededFieldValues

    origin_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="origin",
    )
    destination_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="destination",
    )
    start_date_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="start_date",
    )
    end_date_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="end_date",
    )
    trip_duration_days_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="trip_duration_days",
    )
    num_adults_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="num_adults",
    )
    trip_budget_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="trip_budget",
    )


class OptionalFields(_SourcedRecordSection):
    """Canonical optional section stored as wrapped value/source records."""

    VALUE_MODEL: ClassVar[type[BaseModel]] = OptionalFieldValues

    end_location_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="end_location",
    )
    trip_purpose_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="trip_purpose",
    )
    num_children_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="num_children",
    )
    children_ages_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="children_ages",
    )
    num_seniors_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="num_seniors",
    )
    depart_time_window_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="depart_time_window",
    )
    arrival_time_window_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="arrival_time_window",
    )
    return_time_window_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="return_time_window",
    )
    day_start_time_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="day_start_time",
    )
    day_end_time_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="day_end_time",
    )
    travel_style_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="travel_style",
    )
    spending_tier_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="spending_tier",
    )
    local_transport_modes_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="local_transport_modes",
    )
    long_distance_modes_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="long_distance_modes",
    )
    train_class_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="train_class",
    )
    flight_cabin_class_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="flight_cabin_class",
    )
    activity_interests_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="activity_interests",
    )
    must_see_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="must_see",
    )
    avoid_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="avoid",
    )
    cuisine_prefs_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="cuisine_prefs",
    )
    dietary_restrictions_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="dietary_restrictions",
    )
    accessibility_needs_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="accessibility_needs",
    )
    hotel_tier_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="hotel_tier",
    )
    lodging_type_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="lodging_type",
    )
    room_type_pref_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="room_type_pref",
    )
    trip_notes_record: SourcedValueRecord = Field(
        default_factory=SourcedValueRecord,
        alias="trip_notes",
    )


class PendingClarification(BaseModel):
    """Stored clarification state waiting for a user answer."""

    active: bool = False
    variable: str | None = None
    options: list[str] | None = None
    clarification_question: str | None = None
    proposed_value: Any | None = None


class ConfirmationState(BaseModel):
    """Proof that the current trip details were shown for final approval."""

    active: bool = False
    summary_fingerprint: str | None = None


class EnrichmentState(BaseModel):
    """Tracking state for optional enrichment questions."""

    fields_asked: list[str] = Field(default_factory=list)
    enrichment_complete: bool = False


class PreSummaryGate(BaseModel):
    """Tracks state for pre-summary checks run just before confirmation summary."""

    # Set True when we show a blocking gate message and are waiting for user reply.
    gate_awaiting_response: bool = False

    # Budget feasibility check state.
    budget_check_accepted: bool = False
    # MD5 of the budget-influencing fields when the user last accepted; re-run if changed.
    budget_fields_snapshot: str | None = None

    # Destination accessibility check state.
    accessibility_check_accepted: bool = False
    # MD5 of the destination+date fields when the user last accepted; re-run if changed.
    accessibility_fields_snapshot: str | None = None

    # Booking-window warning text to fold into the summary when generated.
    booking_window_note: str | None = None


class SanityState(BaseModel):
    """Cached sanity-check state reused across collection turns."""

    destination_verified: bool = False
    resolved_city_names: list[str] = Field(default_factory=list)
    # One cached worldwide research result plus the exact inputs that produced it.
    destination_intelligence: dict | None = None
    destination_intelligence_snapshot: str | None = None
    # Pre-summary gate state (budget, accessibility, booking-window).
    pre_summary_gate: PreSummaryGate = Field(default_factory=PreSummaryGate)


class InternalFields(BaseModel):
    """Internal orchestration fields not asked directly from the user."""

    pending_clarification: PendingClarification = Field(
        default_factory=PendingClarification
    )
    confirmation_state: ConfirmationState = Field(default_factory=ConfirmationState)
    enrichment_state: EnrichmentState = Field(default_factory=EnrichmentState)
    sanity_state: SanityState = Field(default_factory=SanityState)
    profile_signals: list[str] = Field(default_factory=list)
    trip_context_notes: list[str] = Field(default_factory=list)
    # The extraction agent's original duration wording (for example
    # "5 nights") is display-only. itinerary planning continues to receive the canonical
    # calendar-day count in ``trip_duration_days``.
    duration_display_label: str | None = None
    data_confidence: DataConfidence | None = None
    correction_count: int = 0
    intent_retry_count: int = 0
    junk_turn_count: int = 0

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_pending_clarification_key(cls, data: Any) -> Any:
        """Read older saved memory while serializing the clearer field name."""

        if isinstance(data, dict):
            migrated = dict(data)
            if (
                "pending_clarification" not in migrated
                and "ambiguity_state" in migrated
            ):
                migrated["pending_clarification"] = migrated["ambiguity_state"]
            return migrated
        return data


class TripMemory(BaseModel):
    """Full persisted canonical trip memory for one conversation."""

    schema_version: Literal["trip_memory.v1"] = "trip_memory.v1"
    metadata: TripMetadata = Field(default_factory=TripMetadata)
    needed: NeededFields = Field(default_factory=NeededFields)
    optional: OptionalFields = Field(default_factory=OptionalFields)
    internal: InternalFields = Field(default_factory=InternalFields)

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the canonical DB/storage shape for this trip memory."""

        return self.model_dump(mode="json", by_alias=True)

    def get_variable_record(self, variable_name: str) -> FieldMeta | None:
        """Return the stored record for one tracked sourced variable when it exists."""

        variable_name = _canonical_variable_name(variable_name)
        for section in (self.needed, self.optional):
            if not section.has_variable(variable_name):
                continue
            record = section.get_record(variable_name)
            if record.value is None and record.source is None:
                return None
            if record.source is None:
                return None
            return FieldMeta(value=record.value, source=record.source)
        return None

    def get_field_value(self, variable_name: str) -> Any:
        """Return one variable value from metadata, needed, or optional sections."""

        variable_name = _canonical_variable_name(variable_name)
        if self.metadata.has_variable(variable_name):
            return self.metadata.get_value(variable_name)
        if self.needed.has_variable(variable_name):
            return self.needed.get_value(variable_name)
        if self.optional.has_variable(variable_name):
            return self.optional.get_value(variable_name)
        return None

    def set_field(
        self,
        variable_name: str,
        value: Any,
        source: FieldSource,
    ) -> None:
        """Set one variable value and persist its normalized source-aware record."""

        variable_name = _canonical_variable_name(variable_name)
        if self.needed.has_variable(variable_name):
            payload = self.needed.model_dump(by_alias=True, mode="python")
            payload[variable_name] = {"value": value, "source": source}
            updated_needed = NeededFields.model_validate(payload)
            self.needed = updated_needed
            return

        if self.optional.has_variable(variable_name):
            payload = self.optional.model_dump(by_alias=True, mode="python")
            payload[variable_name] = {"value": value, "source": source}
            updated_optional = OptionalFields.model_validate(payload)
            self.optional = updated_optional
            return

        if self.metadata.has_variable(variable_name):
            payload = self.metadata.model_dump(by_alias=True, mode="python")
            payload[variable_name] = {"value": value}
            updated_metadata = TripMetadata.model_validate(payload)
            self.metadata = updated_metadata
            return

        raise AttributeError(f"Unknown variable: {variable_name}")

    def clear_field(self, variable_name: str) -> None:
        """Clear one variable value and its stored source metadata."""

        variable_name = _canonical_variable_name(variable_name)
        if self.needed.has_variable(variable_name):
            payload = self.needed.model_dump(by_alias=True, mode="python")
            payload[variable_name] = {"value": None, "source": None}
            self.needed = NeededFields.model_validate(payload)
            return

        if self.optional.has_variable(variable_name):
            payload = self.optional.model_dump(by_alias=True, mode="python")
            payload[variable_name] = {"value": None, "source": None}
            self.optional = OptionalFields.model_validate(payload)
            return

        if self.metadata.has_variable(variable_name):
            payload = self.metadata.model_dump(by_alias=True, mode="python")
            payload[variable_name] = {"value": None}
            self.metadata = TripMetadata.model_validate(payload)

    def get_source_map(self) -> dict[str, str]:
        """Return a simple variable-to-source lookup for sourced variables only."""

        source_map: dict[str, str] = {}
        for section in (self.needed, self.optional):
            for variable_name, record in section.iter_records():
                if record.source is None:
                    continue
                source_map[variable_name] = record.source.value
        return source_map
