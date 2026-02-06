"""Rate limiting middleware with Admin Email and Role bypass."""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timezone
from threading import Lock
from typing import Callable

from fastapi import HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import (
    RATE_LIMIT_IP_REQUESTS_PER_MINUTE,
    RATE_LIMIT_MESSAGES_PER_MINUTE,
    RATE_LIMIT_TRIPS_PER_MONTH,
    get_admin_emails,
)
from app.db.repository import TripRepository


class InMemoryRateLimiter:
    """Thread-safe rate limiter for short IP and user-message windows."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._ip_requests: dict[str, list[float]] = defaultdict(list)
        self._user_messages: dict[int, list[float]] = defaultdict(list)

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

    def is_message_limited(self, user_id: int) -> bool:
        now = time.time()
        window = 60.0
        with self._lock:
            timestamps = [t for t in self._user_messages[user_id] if now - t < window]
            if len(timestamps) >= RATE_LIMIT_MESSAGES_PER_MINUTE:
                self._user_messages[user_id] = timestamps
                return True
            timestamps.append(now)
            self._user_messages[user_id] = timestamps
            return False


limiter = InMemoryRateLimiter()


def _month_start_utc() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _was_created_this_month(created_at: str | None, month_start: datetime) -> bool:
    """Read legacy timestamps defensively so one malformed row never blocks a user."""
    if not created_at:
        return False
    try:
        return datetime.fromisoformat(created_at.replace("Z", "+00:00")) >= month_start
    except ValueError:
        return False


def check_user_trip_limit(
    repository: TripRepository,
    user_id: int,
    user_email: str | None,
    role: str = "user",
) -> None:
    """Raise 429 when a user reaches the configured calendar-month trip cap."""
    if RATE_LIMIT_TRIPS_PER_MONTH <= 0:
        return
    admin_emails = get_admin_emails()
    if role == "admin" or (user_email and user_email.casefold() in admin_emails):
        return

    month_start = _month_start_utc()
    created_this_month = sum(
        1
        for trip in repository.list_trips_by_user(user_id)
        if _was_created_this_month(trip.created_at, month_start)
    )
    if created_this_month >= RATE_LIMIT_TRIPS_PER_MONTH:
        raise HTTPException(
            status_code=429,
            detail=(
                "Monthly trip limit "
                f"({RATE_LIMIT_TRIPS_PER_MONTH} trips/month) reached. Please try again next month."
            ),
        )


def check_user_message_limit(
    user_id: int, user_email: str | None, role: str = "user"
) -> None:
    """Raise 429 if user exceeded chat rate limit (Admin bypasses)."""
    if RATE_LIMIT_MESSAGES_PER_MINUTE <= 0:
        return
    admin_emails = get_admin_emails()
    if role == "admin" or (user_email and user_email.casefold() in admin_emails):
        return  # Admin bypass

    if limiter.is_message_limited(user_id):
        raise HTTPException(
            status_code=429,
            detail="Chat rate limit exceeded. Please wait a few seconds before sending another message.",
        )


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
