"""Health check routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])


@router.get("/api/health")
@router.get("/health")
@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(request: Request) -> JSONResponse:
    settings = request.app.state.planner_settings
    issues = settings.readiness_issues()
    try:
        request.app.state.repository.check_connection()
    except Exception:
        issues.append("Database is unavailable")
    return JSONResponse(
        status_code=200 if not issues else 503,
        content={
            "status": "ready" if not issues else "not_ready",
            "mode": settings.mode,
            "issues": issues,
        },
    )
