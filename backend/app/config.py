"""Unified configuration for the Trip Planner app."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent
ENV_FILE = BACKEND_DIR / ".env"
load_dotenv(ENV_FILE, override=False)


def _configure_openai_proxy_bypass() -> None:
    """Avoid a broken machine proxy for direct OpenAI API traffic.

    Set ``RIYA_OPENAI_BYPASS_PROXY=false`` only when a working corporate proxy
    must handle OpenAI traffic. This affects just ``api.openai.com``; other
    provider traffic keeps its existing network configuration.
    """

    if os.getenv("RIYA_OPENAI_BYPASS_PROXY", "true").strip().casefold() in {
        "0",
        "false",
        "no",
    }:
        return
    for key in ("NO_PROXY", "no_proxy"):
        existing = [
            value.strip() for value in os.getenv(key, "").split(",") if value.strip()
        ]
        if "api.openai.com" not in {value.casefold() for value in existing}:
            os.environ[key] = ",".join([*existing, "api.openai.com"])


_configure_openai_proxy_bypass()


def _env_int(name: str, default: int) -> int:
    """Read a positive integer environment setting with a safe fallback."""
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _env_nonnegative_int(name: str, default: int) -> int:
    """Read an integer setting where zero explicitly disables a limit."""
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value >= 0 else default


TEST_USER_ID = 1
STATE_MESSAGE_LIMIT = _env_int("RIYA_STATE_MESSAGE_LIMIT", 80)


# Rate Limiting & Admin Configuration
def get_admin_emails() -> set[str]:
    raw = os.getenv("ADMIN_EMAILS", "").strip()
    if not raw:
        return set()
    return {email.strip().casefold() for email in raw.split(",") if email.strip()}


RATE_LIMIT_TRIPS_PER_MONTH = _env_nonnegative_int("RATE_LIMIT_TRIPS_PER_MONTH", 3)
# Set to 0 to disable chat-message throttling. Trip creation still has its
# separate daily protection, and assistant/status traffic is never counted.
RATE_LIMIT_MESSAGES_PER_MINUTE = _env_nonnegative_int(
    "RATE_LIMIT_MESSAGES_PER_MINUTE", 0
)
RATE_LIMIT_IP_REQUESTS_PER_MINUTE = _env_int("RATE_LIMIT_IP_REQUESTS_PER_MINUTE", 30)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
DEFAULT_NO_TOOL_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-5.6-luna")
DEFAULT_PIPELINE_QUALITY_MODEL = os.getenv(
    "OPENAI_PIPELINE_QUALITY_MODEL", "gpt-5.6-luna"
)


def get_database_url() -> str:
    """Return the configured online database URL, when one is provided."""
    return os.getenv("DATABASE_URL", "").strip()


def require_database_url() -> str:
    """Return the configured Postgres URL or fail before app startup."""
    url = get_database_url()
    if not url:
        raise RuntimeError("DATABASE_URL is required.")
    return url


def get_database_schema() -> str:
    """Return the Postgres schema used by this process."""
    return os.getenv("RIYA_DATABASE_SCHEMA", "public").strip() or "public"


def _normalize_model_name(value: str, *, responses: bool = False) -> str:
    raw = value.strip()
    if ":" in raw:
        if responses and raw.startswith("openai:"):
            return raw.replace("openai:", "openai-responses:", 1)
        if not responses and raw.startswith("openai:"):
            return raw.replace("openai:", "openai-chat:", 1)
        return raw
    prefix = "openai-responses" if responses else "openai-chat"
    return f"{prefix}:{raw}"


EXTRACTION_MODEL = _normalize_model_name(
    os.getenv("RIYA_EXTRACTION_MODEL", DEFAULT_NO_TOOL_MODEL), responses=True
)
RESPONSE_MODEL = _normalize_model_name(
    os.getenv("RIYA_RESPONSE_MODEL", DEFAULT_NO_TOOL_MODEL),
    responses=True,
)
CONVERSATION_MODEL = _normalize_model_name(
    os.getenv("RIYA_CONVERSATION_MODEL", DEFAULT_NO_TOOL_MODEL), responses=True
)
PREFILL_MODEL = _normalize_model_name(
    os.getenv("RIYA_PREFILL_MODEL", DEFAULT_NO_TOOL_MODEL), responses=True
)
PRE_CLASSIFIER_MODEL = _normalize_model_name(
    os.getenv("RIYA_PRE_CLASSIFIER_MODEL", DEFAULT_NO_TOOL_MODEL), responses=True
)
FEASIBILITY_MODEL = _normalize_model_name(
    os.getenv("RIYA_FEASIBILITY_MODEL", DEFAULT_NO_TOOL_MODEL), responses=True
)
DESTINATION_INTELLIGENCE_MODEL = _normalize_model_name(
    os.getenv("RIYA_PLACE_RESOLUTION_MODEL", DEFAULT_PIPELINE_QUALITY_MODEL),
    responses=True,
)
VALIDATION_MODEL = _normalize_model_name(
    os.getenv("RIYA_VALIDATION_MODEL", DEFAULT_NO_TOOL_MODEL), responses=True
)
INTENT_MODEL = _normalize_model_name(
    os.getenv("RIYA_INTENT_MODEL", DEFAULT_NO_TOOL_MODEL), responses=True
)
PROFILE_UPDATE_MODEL = _normalize_model_name(
    os.getenv("RIYA_PROFILE_UPDATE_MODEL", DEFAULT_NO_TOOL_MODEL), responses=True
)
METADATA_MODEL = _normalize_model_name(
    os.getenv("RIYA_METADATA_MODEL", DEFAULT_NO_TOOL_MODEL), responses=True
)
COMPRESSION_MODEL = _normalize_model_name(
    os.getenv("RIYA_COMPRESSION_MODEL", DEFAULT_NO_TOOL_MODEL), responses=True
)

ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]
_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh", "max"}
_COLLECTION_REASONING_DEFAULTS: dict[str, ReasoningEffort] = {
    # Fast, deterministic routing and concise conversation do not benefit
    # from hidden chain-of-thought work.
    "pre_classifier": "none",
    "conversation": "none",
    "compression": "none",
    "prefill": "none",
    # These transform or assess user details and deserve a small reasoning
    # budget without paying the latency cost of the model default.
    "extraction": "low",
    "feasibility": "low",
    "intent": "low",
    "profile_update": "low",
    "metadata": "low",
    # Destination intelligence can involve nuanced geographic, timing, and
    # travel-safety judgement, so retain the balanced setting.
    "destination_intelligence": "medium",
}


def collection_agent_model_settings(agent_name: str) -> dict[str, str]:
    """Return one explicit, per-agent Responses API reasoning configuration.

    Environment overrides are intentionally agent-scoped so production can
    keep simple routing fast without weakening extraction or trip judgement.
    """

    setting_name = f"RIYA_{agent_name.upper()}_REASONING_EFFORT"
    effort = (
        os.getenv(
            setting_name, _COLLECTION_REASONING_DEFAULTS.get(agent_name, "medium")
        )
        .strip()
        .lower()
    )
    if effort not in _REASONING_EFFORTS:
        effort = "medium"
    return {
        "openai_reasoning_effort": effort,
        "openai_text_verbosity": "low",
    }


@dataclass(frozen=True, slots=True)
class Settings:
    mode: str = "live"
    timeout_seconds: float = 300.0
    openai_api_key: str | None = None
    tavily_api_key: str | None = None
    redis_url: str | None = None
    app_role: str = "all"
    openai_model: str = "gpt-5.6-luna"
    ors_api_key: str | None = None
    google_maps_api_key: str | None = None
    planner_version: str = "0.1.0"
    max_llm_tokens_per_trip: int = 0
    max_tool_calls_per_trip: int = 0
    max_search_calls_per_trip: int = 0
    max_concurrent_planning_jobs: int = 2
    planning_lease_seconds: int = 600
    planning_heartbeat_seconds: int = 15

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> Settings:
        load_dotenv(dotenv_path=env_file, override=False)
        return cls(
            max_concurrent_planning_jobs=_env_int("PLANNER_MAX_CONCURRENT_JOBS", 2),
            planning_lease_seconds=_env_int("PLANNER_JOB_LEASE_SECONDS", 600),
            planning_heartbeat_seconds=_env_int("PLANNER_HEARTBEAT_SECONDS", 15),
            mode=os.getenv("PLANNER_MODE", "live").strip().lower(),
            timeout_seconds=float(os.getenv("PLANNER_TIMEOUT_SECONDS", "300")),
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            tavily_api_key=os.getenv("TAVILY_API_KEY") or None,
            redis_url=os.getenv("REDIS_URL") or None,
            app_role=os.getenv("TRIP_PLANNER_APP_ROLE", "all").strip().lower(),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.6-luna"),
            ors_api_key=os.getenv("ORS_API_KEY") or None,
            google_maps_api_key=(
                os.getenv("GOOGLE_MAPS_API_KEY")
                or os.getenv("GPLACES_API_KEY")
                or os.getenv("GOOGLE_API_KEY")
                or None
            ),
            max_llm_tokens_per_trip=_non_negative_env_int(
                "PLANNER_MAX_LLM_TOKENS_PER_TRIP"
            ),
            max_tool_calls_per_trip=_non_negative_env_int(
                "PLANNER_MAX_TOOL_CALLS_PER_TRIP"
            ),
            max_search_calls_per_trip=_non_negative_env_int(
                "PLANNER_MAX_SEARCH_CALLS_PER_TRIP"
            ),
        )

    def readiness_issues(self) -> list[str]:
        if self.mode == "test":
            return []
        issues: list[str] = []
        if self.mode != "live":
            issues.append("Only live provider mode is supported at runtime")
        if not self.openai_api_key:
            issues.append("OPENAI_API_KEY is not configured")
        if not self.ors_api_key:
            issues.append("ORS_API_KEY is not configured")
        if not self.google_maps_api_key:
            issues.append("GOOGLE_MAPS_API_KEY is not configured")
        return issues


def _non_negative_env_int(name: str) -> int:
    """Read an optional safety limit; zero deliberately means observe-only."""

    try:
        return max(int(os.getenv(name, "0")), 0)
    except ValueError:
        return 0
