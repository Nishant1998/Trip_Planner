"""Production Trip and Chat API endpoints supporting 12-char Hex Slugs and SSE Streaming."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import AuthenticatedUser, current_user, require_trip_owner
from app.api.rate_limit import check_user_message_limit, check_user_trip_limit
from app.collection.runtime import CollectionRuntimeService, TripBusyError
from app.db.repository import TripRepository
from app.db.tables import TripRow
from app.domain.cover_images import destination_cover
from app.domain.security import contains_sensitive_data
from app.models.api import AcceptedMessageResponse
from app.models.enums import TripStatus
from app.models.state import (
    load_runtime_memory,
    serialize_runtime_memory,
)
from app.services.events import TripEventBus, format_sse


WELCOME_MESSAGE = (
    "Hey there! I am Riya, your travel companion. Where are you dreaming of going?"
)


# --- Request & Response Models ---


class TripCardSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    slug: str
    name: str
    description: str | None = None
    cover_image_url: str | None = None
    cover_source_url: str | None = None
    cover_credit: str | None = None
    dates: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    duration_days: int | None = None
    status: str = "Draft"
    factors: list[str] = Field(default_factory=list)
    travelers: dict[str, int] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None
    updated_ago: str = "Just now"


class TripListResponse(BaseModel):
    trips: list[TripCardSummary]


class InitTripRequest(BaseModel):
    text: str | None = None
    client_message_id: str | None = Field(default=None, min_length=1, max_length=128)
    message: dict[str, Any] | None = (
        None  # Compatible with nested { text: "..." } payloads
    )
    welcome_message: str | None = Field(default=None, min_length=1, max_length=300)
    user_id: str | None = None


class ChatSendRequest(BaseModel):
    text: str | None = None
    message: dict[str, Any] | None = None
    client_message_id: str | None = Field(default=None, min_length=1, max_length=128)


class StoredMessageResponse(BaseModel):
    id: str
    role: str
    content: str
    created_at: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


class TripMessagesResponse(BaseModel):
    messages: list[StoredMessageResponse]


# --- Helpers ---


def _extract_text(payload: InitTripRequest | ChatSendRequest) -> str:
    if payload.text and payload.text.strip():
        return payload.text.strip()
    if payload.message:
        if isinstance(payload.message, dict):
            t = payload.message.get("text") or payload.message.get("content")
            if t and str(t).strip():
                return str(t).strip()
    return ""


def validate_user_message(text: str) -> str:
    normalized = text.strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if contains_sensitive_data(normalized):
        raise HTTPException(
            status_code=400,
            detail="Please remove passwords, OTPs, payment details, API keys, or access tokens.",
        )
    return normalized


def require_trip_by_slug_or_id(repository: TripRepository, slug_or_id: str) -> TripRow:
    # Check if slug
    trip = repository.get_trip_by_slug(slug_or_id)
    if trip is None and slug_or_id.isdigit():
        trip = repository.get_trip(int(slug_or_id))
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    return trip


# --- Router Registration ---


def register_trip_routes(
    router: APIRouter,
    repository: TripRepository,
    event_bus: TripEventBus,
    runtime: CollectionRuntimeService,
) -> None:
    """Register full production REST endpoints for Trips & Chat."""

    @router.get("", response_model=TripListResponse)
    @router.get("/list", response_model=TripListResponse)
    async def list_user_trips(
        user: AuthenticatedUser = Depends(current_user),
    ) -> TripListResponse:
        """List all trips owned by the authenticated user with cover images and tags."""
        rows = repository.list_trips_by_user(user.id)
        cards: list[TripCardSummary] = []
        for r in rows:
            dest_name = r.destination_items or r.origin_city or "Dream Vacation"
            cover, cover_source, cover_credit = await destination_cover(dest_name, r.cover_image_url)

            # Format friendly status
            raw_status = (r.status or "draft").replace("_", " ").title()

            # Dates string
            dates_str = None
            if r.start_date and r.end_date:
                dates_str = f"{r.start_date} – {r.end_date}"
            elif r.number_of_days:
                dates_str = f"{r.number_of_days} Days"

            cards.append(
                TripCardSummary(
                    id=str(r.id),
                    slug=r.slug or f"trip-{r.id}",
                    name=r.trip_name or f"Trip to {dest_name}",
                    description=r.trip_description
                    or "Personalized AI-designed itinerary.",
                    cover_image_url=cover,
                    cover_source_url=cover_source,
                    cover_credit=cover_credit,
                    dates=dates_str,
                    start_date=r.start_date,
                    end_date=r.end_date,
                    duration_days=r.number_of_days,
                    status=raw_status,
                    factors=r.factors or [],
                    travelers={
                        "adults": r.travelers_adults,
                        "children": r.travelers_children,
                        "seniors": r.travelers_seniors,
                    },
                    budget={
                        "currency": r.budget_currency,
                        "amount": r.budget_amount,
                    },
                    created_at=r.created_at,
                    updated_at=r.updated_at,
                    updated_ago="Recently",
                )
            )
        return TripListResponse(trips=cards)

    @router.post("/init")
    @router.post("/start")
    async def init_trip(
        payload: InitTripRequest,
        user: AuthenticatedUser = Depends(current_user),
    ) -> dict[str, Any]:
        """Lazy trip creation: starts trip on first message with 12-char hex slug."""
        text = validate_user_message(_extract_text(payload))

        # Enforce rate limit (admin bypass)
        check_user_trip_limit(repository, user.id, user.email, user.role)

        trip = repository.create_trip(user.id)
        welcome_message = (
            payload.welcome_message.strip()
            if payload.welcome_message and payload.welcome_message.strip()
            else WELCOME_MESSAGE
        )

        # Persist the exact greeting the browser already displayed, so the
        # durable conversation never replaces it with different wording.
        repository.insert_message(
            trip.id,
            "assistant",
            welcome_message,
            meta={"branch": "welcome", "defaults_applied": [], "extracted": []},
        )

        # Transition status
        state = repository.get_agent_state(trip.id)
        if state and state.memory:
            memory = load_runtime_memory(state.memory)
            memory.metadata.status = TripStatus.COLLECTING
            repository.save_trip_state(trip.id, serialize_runtime_memory(memory))

        # Submit user message to runtime
        try:
            await runtime.submit_user_message(
                trip.id, text, user_id=user.id,
                client_message_id=payload.client_message_id,
            )
        except TripBusyError as exc:
            raise HTTPException(status_code=409, detail=exc.message) from exc

        return {
            "trip_id": str(trip.id),
            "slug": trip.slug or f"trip-{trip.id}",
            "trip_name": trip.trip_name or "New Adventure",
            "redirect_url": f"/trips/{trip.slug or trip.id}",
        }

    @router.get("/{slug_or_id}")
    def get_trip_detail(
        slug_or_id: str,
        user: AuthenticatedUser = Depends(current_user),
    ) -> dict[str, Any]:
        """Fetch trip metadata and status."""
        trip = require_trip_owner(
            require_trip_by_slug_or_id(repository, slug_or_id), user
        )
        state = repository.get_agent_state(trip.id)
        return {
            "trip": {
                "id": str(trip.id),
                "slug": trip.slug or f"trip-{trip.id}",
                "name": trip.trip_name or "My Trip",
                "description": trip.trip_description,
                "cover_image_url": trip.cover_image_url,
                "status": state.status if state else trip.status,
                "start_date": trip.start_date,
                "end_date": trip.end_date,
                "duration_days": trip.number_of_days,
                "travelers": {
                    "adults": trip.travelers_adults,
                    "children": trip.travelers_children,
                    "seniors": trip.travelers_seniors,
                },
                "budget": {
                    "currency": trip.budget_currency,
                    "amount": trip.budget_amount,
                },
                "factors": trip.factors or [],
                "created_at": trip.created_at,
                "updated_at": trip.updated_at,
                "origin_city": trip.origin_city,
                "destination_type": trip.destination_type,
                "destination_items": json.loads(trip.destination_items)
                if trip.destination_items
                else [],
                "trip_scope": trip.trip_scope,
                "spending_tier": trip.spending_tier,
                "travel_style": trip.travel_style,
                "trip_purpose": trip.trip_purpose,
            }
        }

    @router.delete("/{slug_or_id}")
    async def delete_trip(
        slug_or_id: str,
        user: AuthenticatedUser = Depends(current_user),
    ) -> dict[str, bool]:
        """Delete one trip owned by the authenticated user."""
        trip = require_trip_owner(
            require_trip_by_slug_or_id(repository, slug_or_id), user
        )
        ok = repository.delete_trip(trip.id, user.id)
        return {"success": ok}

    @router.get("/{slug_or_id}/messages", response_model=TripMessagesResponse)
    def get_trip_messages(
        slug_or_id: str,
        user: AuthenticatedUser = Depends(current_user),
    ) -> TripMessagesResponse:
        """Fetch chronological chat message history for restoring UI state."""
        trip = require_trip_owner(
            require_trip_by_slug_or_id(repository, slug_or_id), user
        )
        stored = repository.list_messages(trip.id)
        return TripMessagesResponse(
            messages=[
                StoredMessageResponse(
                    id=str(m.id),
                    role=m.role,
                    content=m.content,
                    created_at=m.created_at or "",
                    meta=m.meta,
                )
                for m in stored
            ]
        )

    @router.post("/{slug_or_id}/chat")
    @router.post("/{slug_or_id}/message")
    async def send_chat_message(
        slug_or_id: str,
        payload: ChatSendRequest,
        user: AuthenticatedUser = Depends(current_user),
    ) -> AcceptedMessageResponse:
        """Submit a user chat message into the Riya collection turn."""
        trip = require_trip_owner(
            require_trip_by_slug_or_id(repository, slug_or_id), user
        )
        text = validate_user_message(_extract_text(payload))

        # Enforce rate limit (admin bypass)
        check_user_message_limit(user.id, user.email, user.role)

        if payload.client_message_id and repository.get_message_by_client_message_id(
            trip.id, payload.client_message_id
        ):
            return AcceptedMessageResponse(trip_id=trip.id, slug=trip.slug or "")

        try:
            await runtime.submit_user_message(
                trip.id,
                text,
                user_id=user.id,
                client_message_id=payload.client_message_id,
            )
        except TripBusyError as exc:
            raise HTTPException(status_code=409, detail=exc.message) from exc

        return AcceptedMessageResponse(
            trip_id=trip.id,
            slug=trip.slug or "",
        )

    @router.get("/{slug_or_id}/stream")
    @router.get("/{slug_or_id}/events")
    async def stream_trip_events(
        slug_or_id: str,
        user: AuthenticatedUser = Depends(current_user),
    ) -> StreamingResponse:
        """Server-Sent Events (SSE) stream for status tags, text chunks, and suggestions."""
        trip = require_trip_owner(
            await asyncio.to_thread(require_trip_by_slug_or_id, repository, slug_or_id), user
        )

        async def event_generator() -> AsyncIterator[str]:
            queue = await event_bus.subscribe(trip.id)
            try:
                processing = runtime.is_processing(trip.id)
                yield format_sse("status", {
                    "status": "processing" if processing else "idle",
                    "message": "Working on your trip details" if processing else "",
                    "turn_complete": not processing,
                })
                checkpoint = await asyncio.to_thread(repository.get_pending_checkpoint, trip.id)
                if checkpoint:
                    yield format_sse("checkpoint", checkpoint.payload)

                while True:
                    try:
                        message = await asyncio.wait_for(queue.get(), timeout=15)
                        yield message
                    except TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                await event_bus.unsubscribe(trip.id, queue)

        return StreamingResponse(event_generator(), media_type="text/event-stream")
