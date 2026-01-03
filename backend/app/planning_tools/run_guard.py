"""Per-planning-run deduplication and bounded agent tool dispatch.

Create a fresh PlanningToolCatalog for each run. This cache is never persisted:
Google Places content must not be treated as an unrestricted reusable dataset.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from .contracts import ToolResult
from .http import unavailable

DEFAULT_LIMITS = {
    "places": 20,
    "maps": 12,
    "weather": 3,
    "transport": 4,
    "accommodation": 4,
}


class ToolRunGuard:
    def __init__(self, limits: dict[str, int] | None = None):
        self.limits = dict(DEFAULT_LIMITS if limits is None else limits)
        if set(self.limits) != set(DEFAULT_LIMITS) or any(value < 0 for value in self.limits.values()):
            raise ValueError("limits must provide a nonnegative cap for each of the five tools")
        self._cache: dict[tuple[str, str], ToolResult] = {}
        self._inflight: dict[tuple[str, str], asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._dispatches: Counter[str] = Counter()
        self._cache_hits: Counter[str] = Counter()
        self._limit_hits: Counter[str] = Counter()

    async def call(self, name: str, query: BaseModel,
                   operation: Callable[[BaseModel], Awaitable[ToolResult]]) -> ToolResult:
        if name not in self.limits:
            raise ValueError("unknown agent tool")
        key = (name, query.model_dump_json())
        async with self._lock:
            if key in self._cache:
                self._cache_hits[name] += 1
                return self._cache[key].model_copy(deep=True)
            task = self._inflight.get(key)
            if task is not None:
                self._cache_hits[name] += 1
            else:
                if self._dispatches[name] >= self.limits[name]:
                    self._limit_hits[name] += 1
                    return unavailable(name, "Per-run tool call limit reached.", "run_limit")
                self._dispatches[name] += 1
                task = asyncio.create_task(operation(query))
                self._inflight[key] = task
        try:
            response = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            async with self._lock:
                if self._inflight.get(key) is task:
                    self._inflight.pop(key, None)
            raise
        async with self._lock:
            # Cache the initial submission (including its job ID) to prevent
            # duplicate paid collections. Pending polls must fetch fresh status.
            resuming = bool(getattr(query, "continuation_token", None) or
                            getattr(query, "continuation_tokens", None))
            pending = bool(response.continuation_token or response.continuation_tokens)
            if not (resuming and pending):
                self._cache[key] = response.model_copy(deep=True)
            self._inflight.pop(key, None)
        return response.model_copy(deep=True)

    def usage(self) -> dict[str, dict[str, int]]:
        """Counts tool dispatches, not billable HTTP requests or dollars."""
        return {name: {
            "dispatches": self._dispatches[name],
            "deduplicated": self._cache_hits[name],
            "limit_hits": self._limit_hits[name],
        } for name in DEFAULT_LIMITS}

    async def close(self):
        """Stop orphaned shielded operations before closing provider clients."""
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self._cache.clear()
