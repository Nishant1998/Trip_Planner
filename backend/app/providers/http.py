"""Small HTTP helper with bounded retries for live providers."""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class ProviderHttpClient:
    def __init__(self, timeout_seconds: float) -> None:
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": "riya-trip-planner/0.1"},
            # The developer machine has a stale system proxy configuration.
            # Provider calls must use the direct network path, just like the
            # OpenAI clients, otherwise route/place lookups fail before the
            # planning engine can evaluate a trip.
            trust_env=False,
        )

    @retry(
        retry=retry_if_exception_type(
            (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError)
        ),
        wait=wait_exponential(multiplier=0.25, min=0.25, max=2),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        response = await self.client.get(url, **kwargs)
        response.raise_for_status()
        return response

    @retry(
        retry=retry_if_exception_type(
            (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError)
        ),
        wait=wait_exponential(multiplier=0.25, min=0.25, max=2),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        response = await self.client.post(url, **kwargs)
        response.raise_for_status()
        return response

    async def close(self) -> None:
        await self.client.aclose()
