"""Small durable queue adapter for itinerary planning planner jobs.

Postgres remains the source of truth. Redis only wakes a worker quickly; if it
is unavailable, workers safely find queued jobs through their periodic database
scan. This keeps local development dependency-free while making the production
path resilient to browser disconnects and Redis restarts.
"""

from __future__ import annotations

import logging
import asyncio

logger = logging.getLogger(__name__)

QUEUE_KEY = "trip_planner:planning_jobs"


class PlanningJobQueue:
    """Notify planner workers about durable database jobs when Redis is enabled."""

    def __init__(self, redis_url: str | None) -> None:
        self._redis_url = redis_url
        self._client = None

    async def enqueue(self, job_id: str) -> None:
        """Wake a worker; never make Redis availability a correctness dependency."""

        client = await self._get_client()
        if client is None:
            return
        try:
            await client.lpush(QUEUE_KEY, job_id)
        except Exception:  # pragma: no cover - network/provider dependent
            logger.warning(
                "Redis queue notification failed; database polling will recover it."
            )

    async def dequeue(self, timeout_seconds: int = 5) -> str | None:
        """Wait briefly for an id, returning ``None`` to trigger a DB recovery scan."""

        client = await self._get_client()
        if client is None:
            await asyncio.sleep(timeout_seconds)
            return None
        try:
            item = await client.brpop(QUEUE_KEY, timeout=timeout_seconds)
        except Exception:  # pragma: no cover - network/provider dependent
            logger.warning("Redis queue read failed; database polling will recover it.")
            await asyncio.sleep(timeout_seconds)
            return None
        if item is None:
            return None
        _queue, job_id = item
        return job_id.decode() if isinstance(job_id, bytes) else str(job_id)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get_client(self):
        if not self._redis_url:
            return None
        if self._client is not None:
            return self._client
        try:
            from redis.asyncio import Redis

            self._client = Redis.from_url(
                self._redis_url, decode_responses=False, socket_connect_timeout=3
            )
            await self._client.ping()
            return self._client
        except Exception:  # pragma: no cover - optional integration
            logger.warning(
                "Redis is unavailable; using durable PostgreSQL polling instead."
            )
            if self._client is not None:
                await self._client.aclose()
            self._client = None
            return None
