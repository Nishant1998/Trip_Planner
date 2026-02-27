"""OpenAI Responses API adapter for the only generative planning stage."""

from __future__ import annotations

import json
from asyncio import sleep
from typing import Any, cast

import httpx
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)

from app.models.draft import DraftResult, ItineraryDraft
from app.models.evidence import PlaceEvidence
from app.models.context import (
    PlanningContext,
    ResolvedDestination,
    TripSkeleton,
)
from app.models.plan import ValidationIssue

SYSTEM_PROMPT = """You draft a practical itinerary from confirmed trip requirements.
Return only the requested structured output.

Rules:
- Treat the provided requirements as final. Never ask the traveller a question.
- Apply every trip_context_note and copy each applied note exactly into
  context_notes_applied.
- Apply the meaning of each note in the actual activities: add rest or free-time
  blocks when requested instead of only acknowledging the note.
- Use only provided evidence_place_id values for named attractions. Never invent IDs.
- Do not repeat an evidence_place_id on different days.
- Respect the day skeleton, activity capacity, date order, route order, dietary
  requirements, must-see items, avoids, and travel style.
- Include at least one evidence-backed destination activity on each normal sightseeing
  day. Arrival and departure days may be travel-only when their capacity is zero.
- Give realistic activity durations and conservative cost ranges in the local currency.
- Scale food, entry, and lodging estimates for the supplied traveller count, and keep the
  upper total within the spendable budget when the requirements make that possible.
- Costs are estimates, not live prices or bookings.
- Do not schedule transport times; deterministic code adds routes and time buffers.
- Every breakfast, lunch, dinner, restaurant, or food block must use kind="meal".
  Never disguise a meal as kind="activity".
- Use kind="free_time" for generic shopping or unstructured exploration that is not
  linked to a supplied evidence_place_id.
- Use normal meal order and leave dinner for the evening, never mid-afternoon.
- Use warnings and assumptions when evidence is incomplete.
"""


class OpenAIItineraryProvider:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float = 90,
    ) -> None:
        self.model_name = model
        self._http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=False,
        )
        self._client = AsyncOpenAI(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=0,
            http_client=self._http_client,
        )

    async def create_draft(
        self,
        context: PlanningContext,
        destination: ResolvedDestination,
        skeleton: TripSkeleton,
        places: list[PlaceEvidence],
    ) -> DraftResult:
        payload = _draft_payload(context, destination, skeleton, places)
        return await self._parse(
            "Create the itinerary draft from this JSON:\n"
            + json.dumps(payload, ensure_ascii=False, default=str)
        )

    async def repair_draft(
        self,
        context: PlanningContext,
        destination: ResolvedDestination,
        skeleton: TripSkeleton,
        places: list[PlaceEvidence],
        draft: ItineraryDraft,
        issues: list[ValidationIssue],
    ) -> DraftResult:
        payload = _draft_payload(context, destination, skeleton, places)
        payload["previous_draft"] = draft.model_dump(mode="json")
        payload["validation_issues"] = [
            issue.model_dump(mode="json") for issue in issues
        ]
        return await self._parse(
            "Repair the previous itinerary so all blocking validation issues are fixed. "
            "Do not change confirmed requirements.\n"
            + json.dumps(payload, ensure_ascii=False, default=str)
        )

    async def _parse(self, user_prompt: str) -> DraftResult:
        request = {
            "model": self.model_name,
            "reasoning": {"effort": "medium"},
            "input": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "text_format": ItineraryDraft,
        }
        for attempt in range(4):
            try:
                response = await self._client.responses.parse(**request)
                break
            except (
                APIConnectionError,
                APITimeoutError,
                InternalServerError,
                RateLimitError,
            ) as error:
                if attempt == 3:
                    raise
                await sleep(
                    10 * (attempt + 1)
                    if isinstance(error, RateLimitError)
                    else 2**attempt
                )
        usage = getattr(response, "usage", None)
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise RuntimeError("OpenAI returned no structured itinerary draft.")
        return DraftResult(
            draft=cast(ItineraryDraft, parsed),
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )

    async def close(self) -> None:
        await self._client.close()


def _draft_payload(
    context: PlanningContext,
    destination: ResolvedDestination,
    skeleton: TripSkeleton,
    places: list[PlaceEvidence],
) -> dict[str, Any]:
    return {
        "trip": {
            "trip_name": context.trip_name,
            "origin": context.origin,
            "destination": destination.model_dump(mode="json"),
            "start_date": context.start_date.isoformat(),
            "end_date": context.end_date.isoformat(),
            "duration_days": context.duration_days,
            "travellers": {
                "adults": context.num_adults,
                "children": context.num_children,
                "children_ages": context.children_ages,
                "seniors": context.num_seniors,
            },
            "budget": str(context.trip_budget),
            "currency_code": context.currency_code,
            "travel_style": context.travel_style,
            "requirements": context.requirements,
            "trip_context_notes": context.trip_context_notes,
        },
        "day_skeleton": skeleton.model_dump(mode="json"),
        "available_places": [place.model_dump(mode="json") for place in places],
    }
