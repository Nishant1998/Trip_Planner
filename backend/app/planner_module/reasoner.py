"""Visible LLM reasoning boundary. It selects research targets from source snippets."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
from openai import AsyncOpenAI
from types import SimpleNamespace
from app.services.llm_usage import (
    ensure_current_trip_can_start_model_call,
    record_current_llm_usage,
)

logger = logging.getLogger(__name__)


class ResearchReasoner:
    """The LLM never supplies factual coordinates, hours, prices, or route times."""

    def __init__(
        self,
        api_key: str,
        reasoning_model: str,
        reasoning_effort: str,
        extraction_model: str,
        extraction_effort: str,
    ) -> None:
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1"),
            max_retries=int(os.getenv("RESEARCH_LLM_MAX_RETRIES", "2")),
            http_client=httpx.AsyncClient(
                timeout=httpx.Timeout(
                    float(os.getenv("LLM_HTTP_TIMEOUT_SECONDS", "60"))
                ),
                trust_env=False,
            ),
        )
        self.reasoning_model, self.reasoning_effort = reasoning_model, reasoning_effort
        self.extraction_model, self.extraction_effort = (
            extraction_model,
            extraction_effort,
        )
        self.metrics = {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "calls_by_model": {},
            "input_tokens_by_model": {},
            "output_tokens_by_model": {},
        }

    def _record_usage(self, model: str, response: Any) -> None:
        usage = getattr(response, "usage", None)
        record_current_llm_usage(
            "research",
            model,
            SimpleNamespace(
                requests=1,
                input_tokens=getattr(usage, "input_tokens", 0),
                output_tokens=getattr(usage, "output_tokens", 0),
                cache_read_tokens=getattr(
                    getattr(usage, "input_tokens_details", None), "cached_tokens", 0
                ),
                details={
                    "reasoning_tokens": getattr(
                        getattr(usage, "output_tokens_details", None),
                        "reasoning_tokens",
                        0,
                    )
                },
            ),
        )
        inputs, outputs = (
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
        )
        self.metrics["calls"] += 1
        logger.debug(
            "reasoner_response_received", extra={"count": self.metrics["calls"]}
        )
        self.metrics["input_tokens"] += inputs
        self.metrics["output_tokens"] += outputs
        for key, value in (
            ("calls_by_model", 1),
            ("input_tokens_by_model", inputs),
            ("output_tokens_by_model", outputs),
        ):
            self.metrics[key][model] = self.metrics[key].get(model, 0) + value

    @classmethod
    def from_environment(cls) -> "ResearchReasoner":
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError(
                "Live broad destination and circuit research requires OPENAI_API_KEY."
            )
        # Separate jobs keep expensive semantic judgement narrow and fact extraction fast.
        return cls(
            key,
            os.getenv(
                "RESEARCH_REASONING_MODEL",
                os.getenv("RESEARCH_SUPERVISOR_MODEL", "gpt-5.6-terra"),
            ),
            os.getenv("RESEARCH_REASONING_EFFORT", "medium"),
            os.getenv(
                "RESEARCH_EXTRACTION_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
            ),
            os.getenv("RESEARCH_EXTRACTION_EFFORT", "medium"),
        )

    async def close(self) -> None:
        await self.client.close()

    async def select_destination_names(
        self,
        *,
        destination_type: str,
        requested_name: str,
        duration_days: int,
        web_results: list[dict[str, Any]],
        trip_context: dict[str, Any],
    ) -> list[str]:
        """Return only names extracted from the supplied sources; provider resolves them next."""
        snippets = [
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "content": str(item.get("content") or item.get("raw_content") or "")[
                    :6000
                ],
            }
            for item in web_results
        ]
        prompt = {
            "task": "Select source-supported destination names to resolve with Google Places.",
            "rules": [
                "Use only names supported by web_results; never invent a place.",
                "Mandatory requirements outrank optional interests.",
                "For a route return its actual complete members, with locality.",
                "For a region select useful areas, not a fictional complete member list.",
                "For a country choose only realistic bases for the duration; avoid excessive transit and hotel changes.",
                "Return no more than 8 names.",
                "Do not state factual operating hours, duration, prices, or route times.",
            ],
            "return_shape": {
                "names": ["source-supported place name with locality when relevant"]
            },
            "destination_type": destination_type,
            "requested_name": requested_name,
            "duration_days": duration_days,
            "trip_context": trip_context,
            "web_results": snippets,
        }
        ensure_current_trip_can_start_model_call()
        response = await self.client.responses.create(
            model=self.reasoning_model,
            reasoning={"effort": self.reasoning_effort},
            input="Return JSON only.\n" + json.dumps(prompt),
            text={"format": {"type": "json_object"}},
        )
        self._record_usage(self.reasoning_model, response)
        try:
            decoded = json.loads(response.output_text)
            values = (
                decoded
                if isinstance(decoded, list)
                else (
                    decoded.get("names", decoded.get("route_members", []))
                    if isinstance(decoded, dict)
                    else []
                )
            )
            names = []
            for value in values:
                if isinstance(value, dict):
                    name = (
                        value.get("name") or value.get("place") or value.get("temple")
                    )
                    locality = value.get("locality") or value.get("location")
                    value = f"{name}, {locality}" if name and locality else name
                if isinstance(value, str) and value.strip():
                    names.append(value.strip())
            return names[:8]
        except (json.JSONDecodeError, AttributeError, TypeError):
            return []

    async def interpret_poi_facts(
        self, records: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Extract planning facts from supplied web evidence, leaving missing values unknown."""
        prompt = {
            "task": "Extract only source-supported planning facts for each POI.",
            "rules": [
                "Use only each POI's web_results.",
                "Never invent a value.",
                "Use null or empty lists when a fact is not supported.",
                "visit_minutes is a source_estimate, not an official fact unless the source says it is.",
                "adult_entry_cost is adult entry only; exclude transport, guides, meals, and upgrades.",
                "Do not convert regular weekly hours into a special-date opening claim.",
            ],
            "return_shape": {
                "poi_id": {
                    "visit_minutes": "integer or null",
                    "minimum_visit_minutes": "integer or null",
                    "recommended_visit_minutes": "integer or null",
                    "maximum_visit_minutes": "integer or null",
                    "queue_or_access_minutes": "integer or null",
                    "adult_entry_cost": "number or null",
                    "entry_currency": "string or null",
                    "reservation_required": "boolean or null",
                    "booking_lead_time": "string or null",
                    "accessibility_notes": ["string"],
                    "restrictions": ["string"],
                    "photography_restrictions": ["string"],
                    "crowd_notes": ["string"],
                    "weather_sensitive": "boolean or null",
                    "special_closure_notes": ["string"],
                    "last_entry_time": "string or null",
                    "unresolved_fields": ["string"],
                }
            },
            "records": records,
        }
        ensure_current_trip_can_start_model_call()
        response = await self.client.responses.create(
            model=self.extraction_model,
            reasoning={"effort": self.extraction_effort},
            input="Return JSON only.\n" + json.dumps(prompt),
            text={"format": {"type": "json_object"}},
        )
        self._record_usage(self.extraction_model, response)
        try:
            value = json.loads(response.output_text)
            return value if isinstance(value, dict) else {}
        except (json.JSONDecodeError, AttributeError):
            return {}
