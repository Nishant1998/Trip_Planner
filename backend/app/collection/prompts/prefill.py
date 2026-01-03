"""Prompt templates for trip bootstrapping from profile data."""

from __future__ import annotations

from app.models.schema import TRIP_FIELD_SCHEMA

PREFILL_ALLOWED_FIELDS = (
    "origin",
    "travel_style",
    "spending_tier",
    "local_transport_modes",
    "long_distance_modes",
    "activity_interests",
    "avoid",
    "cuisine_prefs",
    "dietary_restrictions",
    "accessibility_needs",
    "hotel_tier",
    "lodging_type",
    "room_type_pref",
    "day_start_time",
    "day_end_time",
    "depart_time_window",
    "arrival_time_window",
    "return_time_window",
    "trip_notes",
)


PREFILL_SYSTEM_PROMPT = """
You are a profile prefill agent for a travel planning application.
You read a user's long-term travel profile and extract stable cross-trip defaults.

Rules:
- Only fill fields from the allowed subset in the task prompt.
- Only fill a field when the profile clearly and repeatedly supports it.
- If a preference is conditional on destination, season, or trip type, do not fill the field. Put it in trip_notes instead.
- If the profile contains conflicting evidence for a field, omit that field entirely.
- If the profile is empty or insufficient, return empty prefilled_fields.
- Values must exactly match the field type and allowed options listed. No other values are allowed for enum fields.
- Do not guess. Do not explain. Return valid JSON only.
""".strip()


def _build_field_schema_block() -> str:
    section_lookup = {
        **TRIP_FIELD_SCHEMA["needed"],
        **TRIP_FIELD_SCHEMA["optional"],
    }
    lines = []
    for field_name in PREFILL_ALLOWED_FIELDS:
        schema = section_lookup[field_name]
        field_type = schema["type"]
        options = schema.get("options")
        desc = schema["desc"]
        if options:
            options_str = ", ".join(f'"{option}"' for option in options)
            lines.append(
                f"- {field_name} ({field_type})"
                f" — allowed values: [{options_str}]"
                f" — {desc}"
            )
        else:
            lines.append(f"- {field_name} ({field_type}) — {desc}")
    return "\n".join(lines)


_FIELD_SCHEMA_BLOCK = _build_field_schema_block()


def build_prefill_task_prompt(profile_text: str) -> str:
    """Build the prompt used to prefill likely trip defaults."""

    return (
        "User's long-term travel profile:\n\n"
        f"{profile_text or '(empty)'}\n\n"
        "Extract stable cross-trip defaults from the allowed fields below.\n"
        "Return only fields clearly supported by the profile.\n\n"
        "Allowed fields:\n"
        f"{_FIELD_SCHEMA_BLOCK}\n\n"
        "For each filled field return:\n"
        '- "value": must match the field type and allowed options exactly\n'
        '- "source_hint": one short phrase paraphrasing the profile evidence\n\n'
        'For fields you noticed but chose not to fill, add to "unprefilled_with_reason" '
        'with one of: "single_trip", "conditional", "conflicting", "insufficient".\n\n'
        "Return only this JSON and nothing else:\n"
        "{\n"
        '  "prefilled_fields": {\n'
        '    "field_name": { "value": ..., "source_hint": "..." }\n'
        "  },\n"
        '  "unprefilled_with_reason": { "field_name": "reason" }\n'
        "}"
    )


if __name__ == "__main__":
    print(build_prefill_task_prompt("ABCD"))
