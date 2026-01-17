"""Source-bound numeric interpretations and deterministic daily limit checks."""
from decimal import Decimal, InvalidOperation
from datetime import UTC, datetime, time, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..tools.transport import normalize_mode
from .timeline import local_instant


class DailyLimit(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    constraint_index: int = Field(ge=0)
    source_text: str = Field(min_length=1, max_length=4000)
    value_start: int = Field(ge=0)
    value_end: int = Field(gt=0)
    value: Decimal = Field(ge=0)
    unit: Literal["minutes", "hours", "visits"]
    unit_start: int = Field(ge=0)
    unit_end: int = Field(gt=0)
    metric: Literal["travel_minutes_per_day", "visits_per_day"]
    modes: list[Literal["driving", "taxi", "rental_car", "walking", "bus", "train", "transit", "flight", "cycling", "metro"]] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def compatible_units(self):
        if self.metric == "visits_per_day":
            if self.unit != "visits" or self.modes or self.value != self.value.to_integral_value():
                raise ValueError("Visit limit requires a whole visit count and no travel modes")
        elif self.unit == "visits" or not self.modes:
            raise ValueError("Travel limit requires time units and explicit modes")
        return self


def check_daily_limits(draft, data, step_facts, timelines):
    issues, checks = [], []
    units = {"minute": "minutes", "minutes": "minutes", "min": "minutes",
             "hour": "hours", "hours": "hours", "hr": "hours", "hrs": "hours",
             "visit": "visits", "visits": "visits", "activity": "visits", "activities": "visits"}
    for rule in draft.daily_limits:
        source_id = f"hard:{rule.constraint_index}"

        def issue(code, message, day=None, outcome="unknown"):
            issues.append({"code": code, "message": message, "outcome": outcome,
                           "day_number": day, "constraint_id": source_id})

        source = (data.mandatory_constraints[rule.constraint_index]
                  if rule.constraint_index < len(data.mandatory_constraints) else None)
        try:
            valid = (source == rule.source_text
                and 0 <= rule.value_start < rule.value_end <= len(rule.source_text)
                and 0 <= rule.unit_start < rule.unit_end <= len(rule.source_text)
                and Decimal(rule.source_text[rule.value_start:rule.value_end]) == rule.value
                and units.get(rule.source_text[rule.unit_start:rule.unit_end].casefold()) == rule.unit)
        except InvalidOperation:
            valid = False
        if not valid:
            issue("HARD_LIMIT_SOURCE_INVALID", "Numeric limit must quote the exact confirmed requirement, value and unit.")
            continue
        limit = rule.value * (60 if rule.unit == "hours" else 1)
        modes = {normalize_mode(mode).value for mode in rule.modes}
        for day in draft.days:
            unknown = day.timeline is None
            observed = Decimal(0)
            if rule.metric == "visits_per_day":
                observed = Decimal(len(day.selected_poi_ids))
                if day.timeline:
                    observed = max(observed, Decimal(sum(step.kind == "visit" for step in day.timeline.steps)))
            elif day.timeline:
                try:
                    lower = local_instant(datetime.combine(day.date, time()), day.timeline.timezone)
                    upper = local_instant(datetime.combine(day.date + timedelta(days=1), time()), day.timeline.timezone)
                except (ValueError, KeyError):
                    issue("HARD_DAILY_LIMIT_UNRESOLVED", "Daily timezone boundary is unresolved.", day.day_number)
                    continue
                for source_day in draft.days:
                    rows = {row["id"]: row for row in timelines.get(str(source_day.day_number), {}).get("timeline", [])}
                    for step in source_day.timeline.steps if source_day.timeline else []:
                        if step.kind != "travel":
                            continue
                        row = rows.get(step.id, {})
                        if not row.get("start") or not row.get("end"):
                            unknown = True
                            continue
                        start = datetime.fromisoformat(row["start"]).astimezone(UTC)
                        end = datetime.fromisoformat(row["end"]).astimezone(UTC)
                        overlap = (min(end, upper) - max(start, lower)).total_seconds()
                        if overlap <= 0:
                            continue
                        mode = normalize_mode(step_facts.get(step.id, {}).get("route_mode")).value
                        if mode == "unknown":
                            unknown = True
                        elif mode in modes:
                            observed += Decimal(str(overlap)) / 60
            outcome = "fail" if observed > limit else "unknown" if unknown else "pass"
            checks.append({"constraint_id": source_id, "day_number": day.day_number,
                "metric": rule.metric, "modes": rule.modes, "limit": str(limit),
                "observed_lower_bound": str(observed), "outcome": outcome,
                "basis": "model_interpretation_of_confirmed_requirement"})
            if outcome == "fail":
                issue("HARD_DAILY_LIMIT_EXCEEDED", "The itinerary exceeds a source-linked daily maximum.", day.day_number, "fail")
            elif outcome == "unknown":
                issue("HARD_DAILY_LIMIT_UNRESOLVED", "Missing timeline, mode or duration prevents checking the daily maximum.", day.day_number)
    return issues, checks
