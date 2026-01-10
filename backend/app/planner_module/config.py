from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace

from dotenv import load_dotenv

from app.config import ENV_FILE


@dataclass(frozen=True)
class Settings:
    reasoning_model: str
    reasoning_effort: str
    reviewer_model: str
    reviewer_effort: str
    max_repairs: int = 3
    local_buffer_minutes: int = 15
    transfer_buffer_minutes: int = 30
    senior_rest_minutes: int = 20
    lunch_earliest_minutes: int = 11 * 60 + 30
    lunch_latest_minutes: int = 14 * 60
    lunch_duration_minutes: int = 45
    max_meal_wait_minutes: int = 30
    senior_rest_meal_imminent_minutes: int = 30
    # Geographic fallback only. Explicit Research relationships always win.
    local_attachment_km: float = 25.0
    target_end_slack_minutes: int = 60
    optional_trial_top_n: int = 3
    max_rebalance_moves: int = 1
    planning_agent_runtime_seconds: int = 240

    @classmethod
    def from_environment(cls) -> "Settings":
        load_dotenv(ENV_FILE, override=False)
        settings = cls(
            reasoning_model=os.getenv(
                "PLANNER_REASONING_MODEL",
                os.getenv("RESEARCH_REASONING_MODEL", "gpt-5.6-terra"),
            ),
            reasoning_effort=os.getenv("PLANNER_REASONING_EFFORT", "medium"),
            reviewer_model=os.getenv(
                "PLANNER_REVIEWER_MODEL",
                os.getenv("RESEARCH_EXTRACTION_MODEL", "gpt-5.6-luna"),
            ),
            reviewer_effort=os.getenv("PLANNER_REVIEWER_EFFORT", "medium"),
            local_attachment_km=float(os.getenv("PLANNER_LOCAL_ATTACHMENT_KM", "25")),
            target_end_slack_minutes=int(
                os.getenv("PLANNER_TARGET_END_SLACK_MINUTES", "60")
            ),
            optional_trial_top_n=int(os.getenv("PLANNER_OPTIONAL_TRIAL_TOP_N", "3")),
            max_rebalance_moves=int(os.getenv("PLANNER_MAX_REBALANCE_MOVES", "1")),
            planning_agent_runtime_seconds=int(
                os.getenv("PLANNER_AGENT_RUNTIME_SECONDS", "240")
            ),
            transfer_buffer_minutes=int(
                os.getenv("PLANNER_TRANSFER_BUFFER_MINUTES", "30")
            ),
            lunch_earliest_minutes=int(
                os.getenv("PLANNER_LUNCH_EARLIEST_MINUTES", str(11 * 60 + 30))
            ),
            lunch_latest_minutes=int(
                os.getenv("PLANNER_LUNCH_LATEST_MINUTES", str(14 * 60))
            ),
            lunch_duration_minutes=int(
                os.getenv("PLANNER_LUNCH_DURATION_MINUTES", "45")
            ),
            max_meal_wait_minutes=int(os.getenv("PLANNER_MAX_MEAL_WAIT_MINUTES", "30")),
            senior_rest_meal_imminent_minutes=int(
                os.getenv("PLANNER_SENIOR_REST_MEAL_IMMINENT_MINUTES", "30")
            ),
        )
        if settings.local_attachment_km < 0:
            raise ValueError("PLANNER_LOCAL_ATTACHMENT_KM must be non-negative")
        values = {}
        for item in fields(cls):
            default = getattr(settings, item.name)
            if isinstance(default, int):
                name = "PLANNER_" + item.name.upper()
                values[item.name] = int(os.getenv(name, str(default)))
                if values[item.name] < 0:
                    raise ValueError(f"{name} must be non-negative")
        return replace(settings, **values)
