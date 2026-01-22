"""Immutable, source-linked requirements projected from confirmed planner input."""
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .contracts import fingerprint

CURRENT_VALIDATION_POLICY = "confirmed-constraints-v15"


def current_validation_policy():
    return CURRENT_VALIDATION_POLICY


class ConstraintRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    source: str
    kind: str
    treatment: Literal["hard", "soft"]
    value_json: str
    target_ids: tuple[str, ...] = ()
    applicability: str = "confirmed trip dates"
    criterion: str


class ConstraintLedger(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    input_fingerprint: str
    policy_version: str = Field(default_factory=current_validation_policy)
    records: tuple[ConstraintRecord, ...]


def build_constraint_ledger(data, *, explicit_order=()):
    records = []

    def add(identity, source, kind, value, criterion, treatment="hard", targets=()):
        records.append(ConstraintRecord(id=identity, source=source, kind=kind,
            value_json=json.dumps(value, sort_keys=True, ensure_ascii=False),
            criterion=criterion, treatment=treatment, target_ids=tuple(sorted(targets))))

    add("trip:origin", "/origin", "origin", data.origin, "First traveler state is origin_anchor.")
    add("trip:end", "/end_location", "endpoint", data.end_location or data.origin,
        "Final traveler state is end_anchor at the required location.")
    add("trip:dates", "/start_date,/end_date,/duration_days", "dates",
        [data.start_date, data.end_date, data.duration_days], "Every confirmed date has a feasible represented day.")
    add("trip:travelers", "/travellers", "party", data.travellers,
        "Cost quantities and suitability retain the confirmed party.")
    if data.budget.get("total", data.budget.get("amount")) is not None:
        add("trip:budget", "/budget", "budget", data.budget,
            "Full-trip supported cost bounds fit the stated budget; unknown components cannot certify a fit.")
    for index, requirement in enumerate(data.requirements):
        targets = {item.id for item in [*data.stops, *data.pois] if requirement.id in item.requirement_ids}
        add(f"requirement:{requirement.id}", f"/requirements/{index}", requirement.requirement_type,
            requirement.model_dump(mode="json"), "Validate the mapped destination or required visits; unresolved mapping remains unknown.",
            "hard" if requirement.required else "soft", targets)
    for index, value in enumerate(data.mandatory_constraints):
        add(f"hard:{index}", f"/mandatory_constraints/{index}", "semantic_constraint", value,
            "Requires source-bound semantic interpretation and a supported check; never silently relaxed.")
    for index, value in enumerate(data.travellers.get("accessibility_needs", [])):
        add(f"accessibility:{index}", f"/travellers/accessibility_needs/{index}", "accessibility", value,
            "Selected visits, stays and movements must have applicable accessibility support.")
    if explicit_order:
        add("trip:explicit_order", "confirmed explicit ordering adapter", "ordering", list(explicit_order),
            "Preserve the confirmed ordered subsequence.", targets=explicit_order)
    if data.time_preferences.get("hard_day_end") is not None:
        add("trip:hard_day_end", "/time_preferences/hard_day_end", "daily_deadline",
            data.time_preferences["hard_day_end"],
            "Checked daily arrival must not exceed this trip-local wall time; ambiguous or unknown timing remains unresolved.")
    for name in ("travel_style", "activity_interests", "transport_preferences", "accommodation_preferences"):
        value = getattr(data, name)
        if value:
            add(f"preference:{name}", f"/{name}", "preference", value,
                "Prefer when feasible; never treat as an inferred hard restriction.", treatment="soft")
    return ConstraintLedger(input_fingerprint=fingerprint(data), records=tuple(records))
