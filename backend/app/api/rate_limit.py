"""General IP request throttling; account allowances live in demo_usage."""

from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import RATE_LIMIT_IP_REQUESTS_PER_MINUTE


class InMemoryRateLimiter:
    """Thread-safe limiter for short IP request windows."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._ip_requests: dict[str, list[float]] = defaultdict(list)

    def is_ip_limited(self, ip: str) -> bool:
        now = time.time()
        window = 60.0
        with self._lock:
            timestamps = [t for t in self._ip_requests[ip] if now - t < window]
            if len(timestamps) >= RATE_LIMIT_IP_REQUESTS_PER_MINUTE:
                self._ip_requests[ip] = timestamps
                return True
            timestamps.append(now)
            self._ip_requests[ip] = timestamps
            return False

limiter = InMemoryRateLimiter()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware enforcing IP request throttles on public endpoints."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        client_ip = request.client.host if request.client else "127.0.0.1"
        path = request.url.path

        # Health checks and live trip progress are read-only infrastructure
        # traffic. They must not consume the visitor's general IP quota.
        is_trip_status_route = path.endswith(("/stream", "/events", "/job-status"))
        if (
            path in {"/health", "/api/health"}
            or path.startswith("/docs")
            or path.startswith("/openapi.json")
            or is_trip_status_route
        ):
            return await call_next(request)

        if limiter.is_ip_limited(client_ip):
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests from this IP. Please slow down."},
            )

        return await call_next(request)
