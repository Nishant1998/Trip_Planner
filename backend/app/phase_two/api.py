"""Owner-authorized planning status, durable plan, and explicit retry."""

import asyncio
from typing import Annotated

from app.api.auth import AuthenticatedUser, current_user, require_trip_owner
from app.api.trips import require_trip_by_slug_or_id
from fastapi import Depends, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .display import refresh_display
from .discovery import discovery_plan
from .portfolio import render_plan

CurrentUser = Annotated[AuthenticatedUser, Depends(current_user)]


class DisplayRequest(BaseModel):
    place_ids: list[str] | None = Field(default=None, max_length=10)
    include_photos: bool = False


def register_planning_routes(router, repository, service):
    async def own(slug, user):
        trip = await asyncio.to_thread(require_trip_by_slug_or_id, repository, slug)
        return require_trip_owner(trip, user)

    @router.get("/{slug_or_id}/job-status")
    async def job_status(slug_or_id: str, user: CurrentUser, response: Response):
        trip = await own(slug_or_id, user)
        response.headers["Cache-Control"] = "no-store"
        job = await service.store.status(trip.id)
        if job is None:
            return {"status": "not_started", "enabled": service.enabled}
        stages = {
            "Starting": "starting", "research": "finding_places",
            "allocation": "building_itinerary", "accommodation": "finding_stays",
            "transport": "checking_routes", "schedule": "building_itinerary",
            "review": "reviewing_plan", "presentation": "finalizing", "saving": "saving",
        }
        job["current_stage"] = stages.get(job.get("current_step"), "starting")
        event = getattr(service, "latest_progress", {}).get(trip.id)
        if event and event["run_id"] == job["id"] and job["status"] == "running":
            job.update(current_stage=event["stage"], activity_detail=event["detail"],
                       progress_kind=event["kind"], progress_sequence=event["sequence"])
        return job

    @router.get("/{slug_or_id}/plan")
    async def get_plan(slug_or_id: str, user: CurrentUser, response: Response):
        trip = await own(slug_or_id, user)
        response.headers["Cache-Control"] = "no-store"
        plan = await asyncio.to_thread(repository.get_active_trip_plan, trip.id)
        if not plan:
            read_artifact = getattr(repository, "get_latest_planning_artifact", None)
            if not callable(read_artifact):
                raise HTTPException(404, "No saved plan is available.")
            detail = await asyncio.to_thread(read_artifact, trip.id, "detail_fill")
            payload = getattr(detail, "payload", None)
            if not isinstance(payload, dict) or not isinstance(payload.get("days"), list):
                raise HTTPException(404, "No saved plan is available.")
            governance = await asyncio.to_thread(read_artifact, trip.id, "governance")
            metadata = getattr(governance, "payload", None)
            delivery = metadata.get("delivery_status") if isinstance(metadata, dict) else None
            if delivery not in {"READY", "READY_WITH_WARNINGS", "DRAFT_NEEDS_CONFIRMATION"}:
                delivery = "DRAFT_NEEDS_CONFIRMATION"
            return {
                "plan": {**payload, "delivery_status": delivery.lower()},
                "discovery": None,
                "version": None,
                "display_fields_status": "refresh_required",
                "warnings": ["This saved draft uses the older plan format; confirm its details before booking."],
            }
        display_cache = getattr(service, "display_plans", None)
        fresh = display_cache.get(plan.plan_json.get("plan_id")) if display_cache else None
        payload = fresh or plan.plan_json
        return {
            "plan": payload,
            "discovery": (
                discovery_plan(payload)
                if payload.get("schema_version") == "trip_plan.v2"
                else None
            ),
            "version": plan.version,
            "display_fields_status": "fresh" if fresh else "refresh_required",
            "warnings": [] if fresh else [
                "Provider display fields are not permanently cached. Refresh explicitly before display."
            ],
        }

    @router.post("/{slug_or_id}/planning/retry", status_code=202)
    async def retry(slug_or_id: str, user: CurrentUser):
        trip = await own(slug_or_id, user)
        if not service.enabled:
            raise HTTPException(503, "Planning is disabled pending acceptance.")
        try:
            return await service.start(trip.id, retry=True)
        except ValueError:
            raise HTTPException(
                409, "Confirm valid trip requirements before planning."
            ) from None

    @router.get("/{slug_or_id}/plan/view", response_class=HTMLResponse)
    async def plan_view(slug_or_id: str, user: CurrentUser):
        trip = await own(slug_or_id, user)
        saved = await asyncio.to_thread(repository.get_active_trip_plan, trip.id)
        if not saved:
            raise HTTPException(404, "No saved plan is available.")
        if saved.plan_json.get("schema_version") != "trip_plan.v2":
            raise HTTPException(409, "This older plan is available through the existing plan view.")
        return HTMLResponse(render_plan(saved.plan_json), headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
            "X-Content-Type-Options": "nosniff",
        })

    @router.post("/{slug_or_id}/plan/display")
    async def display(slug_or_id: str, payload: DisplayRequest, user: CurrentUser):
        trip = await own(slug_or_id, user)
        if not service.enabled:
            raise HTTPException(503, "Planning is disabled pending acceptance.")
        plan = await asyncio.to_thread(repository.get_active_trip_plan, trip.id)
        if not plan:
            raise HTTPException(404, "No saved plan is available.")
        catalog = service.catalog_factory()
        try:
            async with asyncio.timeout(60):
                cache = getattr(service, "display_plans", None)
                cached = cache.get(plan.plan_json.get("plan_id")) if cache is not None else None
                result = await refresh_display(
                    cached or plan.plan_json,
                    catalog,
                    place_ids=payload.place_ids,
                    include_photos=payload.include_photos,
                )
                if cache is not None:
                    cache.put(result["plan"])
                return result
        except ValueError:
            raise HTTPException(
                422, "Request at most ten place IDs selected in this plan."
            ) from None
        except TimeoutError:
            raise HTTPException(
                504, "Display refresh timed out; the stored plan is unchanged."
            ) from None
        finally:
            await catalog.close()
