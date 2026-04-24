"""FastAPI application for TripAi."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.middleware import setup_middleware
from app.api.rate_limit import RateLimitMiddleware
from app.api.trips import register_trip_routes
from app.api.users import router as users_router
from app.collection.agents.registry import AgentRegistry
from app.collection.runtime import CollectionRuntimeService
from app.config import ENV_FILE, Settings
from app.db.factory import build_trip_repository
from app.db.repository import TripRepository
from app.itinerary.api import register_planning_routes
from app.itinerary.service import PlanningService
from app.services.events import TripEventBus
from app.services.logging import configure_logging
from fastapi import APIRouter, FastAPI


@dataclass(slots=True)
class AppServices:
    """Shared long-lived services."""

    repository: TripRepository
    event_bus: TripEventBus
    runtime: CollectionRuntimeService
    planning: PlanningService


def create_app(
    agents: Any = None,
    database_schema: str | None = None,
) -> FastAPI:
    """Create and configure the production Trip Planner application."""

    # --- Shared storage and event services ---
    repository = build_trip_repository(schema=database_schema)
    settings = Settings.from_env(ENV_FILE)
    event_bus = TripEventBus()
    planning = PlanningService(repository, event_bus, planning_profile="daywise")

    configure_logging()

    # --- Collection services ---
    runtime = CollectionRuntimeService(
        repository=repository,
        event_bus=event_bus,
        agents=agents or AgentRegistry.build_default(),
        on_trip_confirmed=planning.on_confirmed,
    )

    services = AppServices(
        repository=repository,
        event_bus=event_bus,
        runtime=runtime,
        planning=planning,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if os.getenv("APP_ENV", "development").strip().lower() == "production":
            issues: list[str] = []
            if os.getenv(
                "RIYA_AUTH_MODE", "google"
            ).strip().lower() != "google" or os.getenv(
                "TEST_MODE", "false"
            ).strip().lower() in {"true", "1", "yes", "on"}:
                issues.append(
                    "Production requires Google authentication and TEST_MODE=false"
                )
            if not os.getenv("GOOGLE_OAUTH_CLIENT_ID"):
                issues.append("GOOGLE_OAUTH_CLIENT_ID is required")
            if not os.getenv("CORS_ORIGINS") or "*" in os.getenv("CORS_ORIGINS", ""):
                issues.append("Production requires explicit CORS_ORIGINS")
            if issues:
                raise RuntimeError("; ".join(issues))
        # Schema changes require explicit opt-in; promotion must not migrate Neon.
        if os.getenv("RIYA_AUTO_MIGRATE", "false").lower() == "true":
            repository.init_db()
        auth_mode = os.getenv("RIYA_AUTH_MODE", "google").strip().lower()
        if auth_mode == "development":
            repository.ensure_test_user(int(os.getenv("RIYA_DEV_USER_ID", "1")))

        # Bind services to app state
        app.state.services = services
        app.state.repository = repository
        app.state.event_bus = event_bus
        app.state.runtime = runtime
        app.state.planner_settings = settings
        try:
            yield
        finally:
            await runtime.close()
            await planning.close()

    app = FastAPI(
        title="TripAi API",
        description="Trip-detail collection and itinerary planning",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Middleware (CORS, error handlers, rate limiting)
    setup_middleware(app)
    app.add_middleware(RateLimitMiddleware)

    # Official Production Routes
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(users_router)

    trips_router = APIRouter(prefix="/api/trips", tags=["trips"])
    register_trip_routes(trips_router, repository, event_bus, runtime)
    register_planning_routes(trips_router, repository, planning)
    app.include_router(trips_router)

    return app
