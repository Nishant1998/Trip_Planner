"""Middleware and exception handlers for FastAPI."""

from __future__ import annotations

import os
import time
import logging
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.services.logging import request_id_context


def _new_request_id() -> str:
    return f"req_{uuid4().hex[:16]}"


def setup_middleware(app: FastAPI) -> None:
    """Configure CORS, readiness gates, and error handlers."""

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_credentials=True,
        allow_origins=[
            origin.strip()
            for origin in os.environ.get(
                "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
            ).split(",")
            if origin.strip()
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    request_times: dict[str, deque[float]] = defaultdict(deque)
    max_body_bytes = _positive_env_int("RIYA_MAX_REQUEST_BYTES", 32_000)
    requests_per_minute = _positive_env_int("RIYA_REQUESTS_PER_MINUTE", 60)
    allowed_origins = {
        origin.strip().rstrip("/")
        for origin in os.environ.get(
            "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
        ).split(",")
        if origin.strip()
    }

    @app.middleware("http")
    async def apply_basic_request_limits(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Bound untrusted request size and burst rate before any AI work starts."""

        if request.method in {"POST", "PUT", "PATCH"}:
            # Cookie-authenticated browser requests must originate from a
            # configured UI. This complements SameSite cookies for mutations.
            origin = request.headers.get("origin", "").rstrip("/")
            is_cookie_session = bool(request.cookies.get("riya_session"))
            if origin and is_cookie_session and origin not in allowed_origins:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Request origin is not allowed"},
                )
            try:
                size = int(request.headers.get("content-length", "0") or 0)
            except ValueError:
                return JSONResponse(
                    status_code=400, content={"detail": "Invalid Content-Length"}
                )
            if size > max_body_bytes:
                return JSONResponse(
                    status_code=413, content={"detail": "Request is too large"}
                )
            chunks = []
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > max_body_bytes:
                    return JSONResponse(
                        status_code=413, content={"detail": "Request is too large"}
                    )
                chunks.append(chunk)
            # Starlette's cached request replays this bounded body downstream.
            request._body = b"".join(chunks)
        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        if len(request_times) > 10000:
            for key in list(request_times):
                if not request_times[key] or now - request_times[key][-1] >= 60:
                    del request_times[key]
        window = request_times[client]
        if len(request_times) > 10000:
            request_times.pop(next(iter(request_times)))
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= requests_per_minute:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests; please retry shortly"},
            )
        window.append(now)
        request_id = _new_request_id()
        token = request_id_context.set(request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            logging.getLogger(__name__).info(
                "request_completed",
                extra={
                    "request_id": request_id,
                    "status": response.status_code,
                    "duration_ms": round((time.monotonic() - now) * 1000),
                },
            )
            return response
        finally:
            request_id_context.reset(token)

    # Validation error handler
    @app.exception_handler(RequestValidationError)
    async def handle_validation(
        _request: Request,
        _error: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": "Request validation failed"},
        )


def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default
