"""HTTP boundary with explicit error results and no credential logging."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from .contracts import ToolResult, ToolStatus


def result(data, provider: str, *, warnings: list[str] | None = None):
    return ToolResult(
        status=ToolStatus.PARTIAL if warnings else ToolStatus.OK,
        data=data, provider=provider, retrieved_at=datetime.now(UTC),
        warnings=warnings or [],
    )


def unavailable(provider: str, message: str, code: str = "not_configured"):
    return ToolResult(
        status=ToolStatus.UNAVAILABLE, provider=provider,
        retrieved_at=datetime.now(UTC), warnings=[message], error_code=code,
    )


def failure(provider: str, exc: Exception):
    if isinstance(exc, httpx.TimeoutException):
        code = "timeout"
    elif isinstance(exc, httpx.HTTPStatusError):
        code = f"http_{exc.response.status_code}"
    elif isinstance(exc, httpx.RequestError):
        code = "network_error"
    else:
        code = "invalid_response"
    return ToolResult(
        status=ToolStatus.ERROR, provider=provider,
        retrieved_at=datetime.now(UTC),
        warnings=[f"{provider} request failed ({code})."], error_code=code,
    )


class HttpTool:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owned = client is None
        self.client = client or httpx.AsyncClient(timeout=15, trust_env=False)

    async def close(self) -> None:
        if self._owned:
            await self.client.aclose()
