"""Small bounded cache, budget, and metrics helpers for all provider calls."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from contextlib import contextmanager
import json
import os
import logging
from pathlib import Path
import sqlite3
import threading
from time import monotonic
from typing import Any, Awaitable, Callable

from app.planner_module.models.research import ResearchRunMetrics

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResearchLimits:
    max_llm_calls: int = 4
    # These are safety ceilings, not the desired research depth. The researcher
    # chooses a duration-aware subset within them.
    max_poi_candidates_per_stop: int = 24
    max_enriched_pois_per_stop: int = 24
    min_actual_pois_per_major_base: int = 3
    max_optional_pois_total: int = 24
    max_optional_pois_per_category: int = 2
    max_discovery_queries_total: int = 8
    max_web_searches: int = 12
    max_google_searches: int = 20
    max_place_details_calls: int = 24
    max_route_calls: int = 12
    max_weather_requests: int = 4
    max_currency_requests: int = 1
    max_concurrent_requests: int = 4
    node_timeout_seconds: float = 20.0
    max_gap_repair_passes: int = 1
    # Empty when tools are constructed directly by a provider-free unit test.
    # ``from_environment`` deliberately defaults live runs to zero metered calls
    # until an operator explicitly chooses a safe allowance.
    provider_daily_limits: dict[str, int] = field(default_factory=dict)
    provider_monthly_limits: dict[str, int] = field(default_factory=dict)
    persistent_store_path: str | None = None

    @classmethod
    def from_environment(cls):
        defaults = cls()
        values = {}
        for item in fields(cls):
            if item.name in {
                "provider_daily_limits",
                "provider_monthly_limits",
                "persistent_store_path",
            }:
                continue
            default = getattr(defaults, item.name)
            name = "RESEARCH_" + item.name.upper()
            value = type(default)(os.getenv(name, str(default)))
            if value < 0 or (
                item.name in {"max_concurrent_requests", "node_timeout_seconds"}
                and value == 0
            ):
                raise ValueError(f"{name} has an invalid limit")
            values[item.name] = value
        metered = {
            "google_text_search": 0,
            "google_place_details": 0,
            "web_search": 0,
            "routes": 0,
            "weather": 0,
            "currency": 0,
        }
        def mapping(name: str) -> dict[str, int]:
            raw = os.getenv(name)
            if raw is None:
                return dict(metered)
            parsed = json.loads(raw)
            if not isinstance(parsed, dict) or any(not isinstance(value, int) or value < 0 for value in parsed.values()):
                raise ValueError(f"{name} must be a JSON object of non-negative integer limits")
            return {str(key): int(value) for key, value in parsed.items()}
        values["provider_daily_limits"] = mapping("RESEARCH_PROVIDER_DAILY_LIMITS")
        values["provider_monthly_limits"] = mapping("RESEARCH_PROVIDER_MONTHLY_LIMITS")
        values["persistent_store_path"] = os.getenv(
            "RESEARCH_PERSISTENCE_PATH", "runtime/research_provider_state.sqlite"
        )
        return cls(**values)


@dataclass
class PersistentResearchStore:
    """Small SQLite persistence for reusable facts and provider quota guards.

    It deliberately stores only JSON provider responses keyed by a normalized
    research key.  Credentials, raw request headers, and user identity never
    enter this database.
    """

    path: str | None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if not self.path:
            return
        location = Path(self.path)
        location.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS research_cache (cache_key TEXT PRIMARY KEY, expires_at REAL NOT NULL, payload TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS research_provider_quota (provider TEXT NOT NULL, period TEXT NOT NULL, calls INTEGER NOT NULL, PRIMARY KEY(provider, period))"
            )

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(str(self.path), timeout=10)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def get(self, key: str) -> object | None:
        if not self.path:
            return None
        now = datetime.now(UTC).timestamp()
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM research_cache WHERE cache_key = ? AND expires_at > ?",
                (key, now),
            ).fetchone()
            connection.execute("DELETE FROM research_cache WHERE expires_at <= ?", (now,))
        return json.loads(row[0]) if row else None

    def set(self, key: str, ttl_seconds: int, value: object) -> None:
        if not self.path or value is None:
            return
        try:
            payload = json.dumps(value, separators=(",", ":"))
        except (TypeError, ValueError):
            return
        expires_at = datetime.now(UTC).timestamp() + ttl_seconds
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO research_cache(cache_key, expires_at, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET expires_at=excluded.expires_at, payload=excluded.payload",
                (key, expires_at, payload),
            )

    def claim(self, provider: str, *, daily_limit: int | None, monthly_limit: int | None) -> None:
        if not self.path or (daily_limit is None and monthly_limit is None):
            return
        now = datetime.now(UTC)
        periods = ((f"day:{now:%Y-%m-%d}", daily_limit), (f"month:{now:%Y-%m}", monthly_limit))
        with self._lock, self._connection() as connection:
            for period, limit in periods:
                if limit is None:
                    continue
                row = connection.execute(
                    "SELECT calls FROM research_provider_quota WHERE provider = ? AND period = ?",
                    (provider, period),
                ).fetchone()
                if (row[0] if row else 0) >= limit:
                    raise RuntimeError(f"Persistent provider quota exhausted for {provider}: {period}")
            for period, limit in periods:
                if limit is not None:
                    connection.execute(
                        "INSERT INTO research_provider_quota(provider, period, calls) VALUES (?, ?, 1) "
                        "ON CONFLICT(provider, period) DO UPDATE SET calls=calls+1",
                        (provider, period),
                    )


@dataclass
class Cache:
    values: dict[str, tuple[float, Any]] = field(default_factory=dict)
    metrics: ResearchRunMetrics = field(default_factory=ResearchRunMetrics)
    persistent: PersistentResearchStore | None = None

    async def get_or_set(
        self, key: str, ttl_seconds: int, factory: Callable[[], Awaitable[Any]]
    ) -> Any:
        current = self.values.get(key)
        if current and current[0] > monotonic():
            logger.debug("cache_hit")
            self.metrics.cache_hits += 1
            return current[1]
        persisted = self.persistent.get(key) if self.persistent else None
        if persisted is not None:
            self.values[key] = (monotonic() + ttl_seconds, persisted)
            self.metrics.cache_hits += 1
            logger.debug("persistent_cache_hit")
            return persisted
        self.metrics.cache_misses += 1
        logger.debug("cache_miss")
        value = await factory()
        self.values[key] = (monotonic() + ttl_seconds, value)
        if self.persistent:
            self.persistent.set(key, ttl_seconds, value)
        return value


@dataclass
class ProviderBudget:
    limits: ResearchLimits
    metrics: ResearchRunMetrics
    persistent: PersistentResearchStore | None = None

    def count(self, provider: str, maximum: int) -> None:
        used = self.metrics.provider_requests.get(provider, 0)
        if used >= maximum:
            logger.warning(
                "provider_budget_exhausted", extra={"provider": provider, "count": used}
            )
            raise RuntimeError(
                f"Provider budget exhausted for {provider}: {maximum} requests"
            )
        if self.persistent:
            self.persistent.claim(
                provider,
                daily_limit=self.limits.provider_daily_limits.get(provider),
                monthly_limit=self.limits.provider_monthly_limits.get(provider),
            )
        self.metrics.provider_requests[provider] = used + 1


@dataclass(frozen=True)
class PricingConfig:
    """Editable optional prices. Null values intentionally keep estimates unavailable."""

    llm_input_per_1k_usd: float | None = None
    llm_output_per_1k_usd: float | None = None
    provider_request_usd: dict[str, float] = field(default_factory=dict)

    def estimate(self, metrics: ResearchRunMetrics) -> float | None:
        if self.llm_input_per_1k_usd is None or self.llm_output_per_1k_usd is None:
            return None
        total = (
            metrics.llm_input_tokens / 1000 * self.llm_input_per_1k_usd
            + metrics.llm_output_tokens / 1000 * self.llm_output_per_1k_usd
        )
        return round(
            total
            + sum(
                self.provider_request_usd.get(name, 0) * count
                for name, count in metrics.provider_requests.items()
            ),
            6,
        )
