"""Adapters between canonical trip state, legacy state, and agent-facing views."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Literal

from pydantic import TypeAdapter

from app.models.enums import FieldSource
from app.models.memory import TripMemory
from app.models.schema import TRIP_FIELD_SCHEMA

AgentViewStage = Literal["collection", "planning", "defaults"]
_LEGACY_VIEW_ALIASES = {
    "total_budget": ("needed", "trip_budget"),
}


def translate_source_map_for_agent(source_map: dict[str, str]) -> dict[str, str]:
    """Return the source map without renaming variable names."""

    return dict(source_map)


def initialize_trip_state(*, slug: str | None = None) -> dict[str, Any]:
    """Create the initial canonical trip state stored in the database."""

    state = TripMemory().to_canonical_dict()
    state["field_metadata"] = {}
    if slug is not None:
        state["metadata"]["slug"]["value"] = slug
    return state


def get_agent_view(
    memory: TripMemory,
    field_name: str,
    stage: AgentViewStage,
) -> dict[str, Any]:
    """Return the merged agent view for one schema field and one stage."""

    section = _find_schema_section(field_name)
    if section is None:
        raise KeyError(f"Unknown trip schema field: {field_name}")

    schema = TRIP_FIELD_SCHEMA[section][field_name]
    field_value = TypeAdapter(Any).dump_python(
        memory.get_field_value(field_name),
        mode="json",
    )

    agent_view = {
        "value": field_value,
        "type": schema["type"],
        "options": deepcopy(schema["options"]),
        "desc": schema["desc"],
    }

    if stage == "defaults" and "default" in schema:
        agent_view["default"] = deepcopy(schema["default"])

    if stage == "collection" and section in {"needed", "optional"}:
        meta = memory.get_variable_record(field_name)
        agent_view["source"] = meta.source.value if meta is not None else None

    return agent_view


def build_agent_memory_view(
    memory: TripMemory,
    stage: AgentViewStage,
) -> dict[str, Any]:
    """Build the full stage-specific agent/view shape from canonical trip memory."""

    agent_view = {
        "metadata": {},
        "needed": {},
        "optional": {},
        "internal": memory.internal.model_dump(mode="json"),
    }

    for section in ("metadata", "needed", "optional"):
        for field_name in TRIP_FIELD_SCHEMA[section]:
            agent_view[section][field_name] = get_agent_view(memory, field_name, stage)

    for alias_name, (section, canonical_name) in _LEGACY_VIEW_ALIASES.items():
        agent_view[section][alias_name] = deepcopy(agent_view[section][canonical_name])

    return agent_view


def build_extraction_current_values(memory: TripMemory) -> dict[str, Any]:
    """Build the lean current-values payload used by the extraction agent."""

    current_values: dict[str, Any] = {}
    for section in ("needed", "optional"):
        for field_name in TRIP_FIELD_SCHEMA[section]:
            field_view = get_agent_view(memory, field_name, "collection")
            field_view.pop("source", None)
            current_values[field_name] = field_view
    return current_values


def extract_lean_state(rich_state: dict[str, Any]) -> dict[str, Any]:
    """Convert a schema-enriched agent view back into canonical trip state shape."""

    lean_state = {
        "metadata": {},
        "needed": {},
        "optional": {},
        "internal": deepcopy(rich_state.get("internal", {})),
    }

    for field_name in TRIP_FIELD_SCHEMA["metadata"]:
        field_data = rich_state.get("metadata", {}).get(field_name, {})
        lean_state["metadata"][field_name] = {"value": field_data.get("value")}

    for section in ("needed", "optional"):
        for field_name in TRIP_FIELD_SCHEMA[section]:
            field_data = rich_state.get(section, {}).get(field_name, {})
            lean_state[section][field_name] = {
                "value": field_data.get("value"),
                "source": field_data.get("source"),
            }

    for alias_name, (section, _canonical_name) in _LEGACY_VIEW_ALIASES.items():
        field_data = rich_state.get(section, {}).get(alias_name, {})
        if field_data:
            lean_state[section][alias_name] = {
                "value": field_data.get("value"),
                "source": field_data.get("source"),
            }

    return lean_state


def apply_defaults(lean_state: dict[str, Any]) -> dict[str, Any]:
    """Fill empty optional fields from schema defaults using source=default."""

    updated = deepcopy(lean_state)
    optional_state = updated.setdefault("optional", {})

    for field_name, schema in TRIP_FIELD_SCHEMA["optional"].items():
        field_data = optional_state.setdefault(
            field_name,
            {"value": None, "source": None},
        )
        if field_data.get("source") == FieldSource.USER.value:
            continue
        if field_data.get("value") is not None:
            continue
        if "default" not in schema or schema["default"] is None:
            continue
        field_data["value"] = deepcopy(schema["default"])
        field_data["source"] = FieldSource.DEFAULT.value

    return updated


def load_runtime_memory(raw_state: str | dict[str, Any] | None) -> TripMemory:
    """Load canonical or legacy trip state into the runtime TripMemory object."""

    if raw_state is None or raw_state == "":
        return TripMemory()

    payload = (
        json.loads(raw_state) if isinstance(raw_state, str) else deepcopy(raw_state)
    )
    if not isinstance(payload, dict):
        return TripMemory()

    metadata_status = payload.get("metadata", {}).get("status")
    if isinstance(metadata_status, dict):
        return TripMemory.model_validate(payload)

    defaults = TripMemory().to_canonical_dict()
    canonical_payload: dict[str, Any] = {
        "metadata": {},
        "needed": {},
        "optional": {},
        "internal": deepcopy(payload.get("internal", defaults["internal"])),
    }

    legacy_field_metadata = deepcopy(payload.get("field_metadata", {}))

    for field_name in TRIP_FIELD_SCHEMA["metadata"]:
        entry = payload.get("metadata", {}).get(
            field_name, defaults["metadata"][field_name]
        )
        canonical_payload["metadata"][field_name] = {
            "value": _extract_field_value(entry),
        }

    for section in ("needed", "optional"):
        for field_name in TRIP_FIELD_SCHEMA[section]:
            entry = payload.get(section, {}).get(field_name)
            source = _extract_field_source(entry)
            if source is None and field_name in legacy_field_metadata:
                source = _extract_field_source(legacy_field_metadata[field_name])
            canonical_payload[section][field_name] = {
                "value": _extract_field_value(entry),
                "source": source,
            }

    return TripMemory.model_validate(canonical_payload)


def serialize_runtime_memory(memory: TripMemory) -> str:
    """Serialize runtime memory into the canonical JSON stored in the database."""

    return json.dumps(memory.to_canonical_dict(), ensure_ascii=False)


def serialize_agent_memory_view(memory: TripMemory, stage: AgentViewStage) -> str:
    """Serialize one stage-specific schema-enriched view to JSON."""

    return json.dumps(build_agent_memory_view(memory, stage), ensure_ascii=False)


def _find_schema_section(field_name: str) -> str | None:
    for section in ("metadata", "needed", "optional"):
        if field_name in TRIP_FIELD_SCHEMA[section]:
            return section
    return None


def _extract_field_value(entry: Any) -> Any:
    if isinstance(entry, dict) and "value" in entry:
        return entry.get("value")
    return entry


def _extract_field_source(entry: Any) -> str | None:
    if isinstance(entry, dict):
        return entry.get("source")
    return None
