"""Serializable attempt ledger with process-local atomic reservations."""
import asyncio
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ResourceLimit(RuntimeError):
    pass


class PlannerLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    model: int = Field(default=10, ge=1)
    utility: int = Field(default=32, ge=1)
    route: int = Field(default=32, ge=0)
    search: int = Field(default=3, ge=0)
    extract: int = Field(default=5, ge=0)
    repairs: int = Field(default=2, ge=0)
    model_seconds: float = Field(default=75, gt=0)
    provider_seconds: float = Field(default=12, gt=0)
    web_seconds: float = Field(default=45, ge=0)


class ResourceLedger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deadline: datetime
    reserve_seconds: float = Field(default=30, gt=0)
    attempts: dict[str, int] = Field(default_factory=dict)
    web_service_seconds: float = Field(default=0, ge=0)
    web_reserved_seconds: float = Field(default=0, ge=0)
    finalized: bool = False
    cancelled: bool = False
    limits: PlannerLimits = Field(default_factory=PlannerLimits)

    @classmethod
    def start(cls, *, seconds=270, reserve=30, now=None, limits=None):
        if seconds <= reserve or reserve <= 0:
            raise ValueError("Runtime must exceed a positive finalization reserve")
        now = now or datetime.now(UTC)
        return cls(deadline=now + timedelta(seconds=seconds), reserve_seconds=reserve,
                   limits=limits or PlannerLimits())


class ResourceGuard:
    """Rebuild on resume around persisted counters; never reset the deadline."""

    def __init__(self, ledger: ResourceLedger, *, clock=lambda: datetime.now(UTC)):
        if ledger.deadline.tzinfo is None:
            raise ValueError("Resource deadline requires timezone")
        self.ledger = ledger
        self.clock = clock
        self.lock = asyncio.Lock()
        self.limits = {key: getattr(ledger.limits, key) for key in ("model", "utility", "route", "search", "extract")}

    def remaining(self):
        if self.ledger.cancelled:
            raise ResourceLimit("cancelled")
        if self.ledger.finalized:
            raise ResourceLimit("finalizing")
        remaining = (self.ledger.deadline - self.clock()).total_seconds() - self.ledger.reserve_seconds
        if remaining <= 0:
            raise ResourceLimit("deadline")
        return remaining

    async def reserve(self, kind: Literal["model", "utility", "route", "search", "extract"]):
        async with self.lock:
            remaining = self.remaining()
            if kind not in self.limits:
                raise ValueError("Unknown resource class")
            used = self.ledger.attempts.get(kind, 0)
            if used >= self.limits[kind]:
                raise ResourceLimit(f"{kind}_quota")
            timeout = min(self.ledger.limits.model_seconds if kind == "model" else self.ledger.limits.provider_seconds, remaining)
            if kind in {"search", "extract"}:
                timeout = min(timeout, self.ledger.limits.web_seconds - self.ledger.web_service_seconds - self.ledger.web_reserved_seconds)
                if timeout <= 0:
                    raise ResourceLimit("web_time")
                self.ledger.web_reserved_seconds += timeout
            self.ledger.attempts[kind] = used + 1
            return timeout

    async def reconcile_web(self, reserved: float, elapsed: float):
        async with self.lock:
            if not 0 <= reserved <= self.ledger.web_reserved_seconds or elapsed < 0:
                raise ValueError("Invalid web reservation reconciliation")
            self.ledger.web_reserved_seconds -= reserved
            self.ledger.web_service_seconds += elapsed
