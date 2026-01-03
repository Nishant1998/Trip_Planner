"""Post-confirmation helpers that update profiles and trip metadata."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date
from typing import Any

from app.collection.prompts.metadata import build_metadata_task_prompt
from app.collection.prompts.profile_update import build_profile_update_task_prompt
from app.db.repository import TripRepository
from app.exceptions import AgentRunError
from app.models.memory import TripMemory
from app.models.state import (
    load_runtime_memory,
    serialize_agent_memory_view,
    serialize_runtime_memory,
)
from app.services.llm_usage import capture_llm_usage

logger = logging.getLogger(__name__)

_EVIDENCE_TAG = re.compile(
    r"\[source=(explicit|observed), confidence=(high|medium), "
    r"last_seen=(\d{4}-\d{2})(?:, evidence=(\d+) trips)?\]$"
)


class PostConfirmationService:
    """Run the non-blocking updates triggered after trip confirmation."""

    def __init__(self, repository: TripRepository, agents: Any) -> None:
        self.repository = repository
        self.agents = agents

    async def dispatch(self, trip_id: int) -> None:
        """Run background updates in an independent token-usage scope."""

        trip = self.repository.get_trip(trip_id)
        if trip is None:
            return
        with capture_llm_usage(
            self.repository,
            user_id=trip.owner_id,
            trip_id=trip_id,
            phase="post_confirmation",
            force_new=True,
        ):
            await self._dispatch(trip_id)

    async def _dispatch(self, trip_id: int) -> None:
        """Run profile and metadata updates after a trip is confirmed."""

        trip = self.repository.get_trip(trip_id)
        if trip is None or not trip.agent_state:
            return

        memory = load_runtime_memory(trip.agent_state)
        profile = self.repository.get_user_profile_obj(trip.owner_id)
        current_full = profile.full_text if profile else ""

        # Collection has already isolated verbatim durable-profile candidates.
        profile_signals = memory.internal.profile_signals or []
        today_month = date.today().strftime("%Y-%m")

        profile_result, _ = await asyncio.gather(
            self._run_profile_update(
                memory, current_full, profile_signals, today_month
            ),
            # Description persistence remains independent from profile persistence.
            self._run_trip_description_generation(trip.id),
            return_exceptions=True,
        )

        merged_full = current_full
        if isinstance(profile_result, str) and profile_result.strip():
            merged_full = profile_result

        if merged_full != current_full:
            self.repository.update_user_profile_full(trip.owner_id, merged_full)
            await self._regenerate_compressed_profile(trip.owner_id, merged_full)

    async def _run_profile_update(
        self,
        memory: TripMemory,
        current_full_profile: str,
        profile_signals: list[str],
        today_month: str,
    ) -> str:
        """Apply the structured post-trip profile delta to the full profile."""

        try:
            delta = await self.agents.profile_update.run(
                build_profile_update_task_prompt(
                    confirmed_memory=self._compact_profile_evidence(memory),
                    current_full_profile=current_full_profile,
                    profile_signals=profile_signals,
                    today_month=today_month,
                )
            )
            return self._apply_profile_delta(
                current_full_profile,
                delta,
                expected_month=today_month,
            )
        except AgentRunError as exc:
            logger.error("Profile update failed: %s", exc)
            return ""

    @staticmethod
    def _compact_profile_evidence(memory: TripMemory) -> str:
        """Keep confirmed non-default values without schema or internal state."""

        canonical = memory.to_canonical_dict()
        evidence: dict[str, dict[str, Any]] = {}
        for section_name in ("needed", "optional"):
            section: dict[str, Any] = {}
            for field_name, record in canonical[section_name].items():
                if not isinstance(record, dict) or record.get("value") is None:
                    continue
                if record.get("source") == "default":
                    continue
                section[field_name] = record
            if section:
                evidence[section_name] = section

        currency = memory.metadata.currency_code
        if currency:
            evidence["budget_currency"] = {"value": currency}
        return json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))

    async def _run_trip_description_generation(self, trip_id: int) -> None:
        """Generate the final trip description after confirmation.

        Loads a fresh copy of the trip state from the database so it reads
        the fully-confirmed memory (finalize_turn has already completed by
        the time dispatch() is called). The trip title was already set by
        Python during collection; geography comes from destination research.
        """
        try:
            trip = self.repository.get_trip(trip_id)
            if trip is None or not trip.agent_state:
                logger.warning(
                    "_run_trip_description_generation: trip %s not found", trip_id
                )
                return

            memory = load_runtime_memory(trip.agent_state)
            result = await self.agents.metadata.run(
                build_metadata_task_prompt(
                    serialize_agent_memory_view(memory, "planning")
                )
            )

            memory.metadata.trip_description = result.trip_description
            trip_name = getattr(result, "trip_name", None)
            if trip_name:
                memory.metadata.trip_name = trip_name
            self.repository.save_trip_state(trip_id, serialize_runtime_memory(memory))
            logger.info(
                "Trip description generated: name=%s trip_id=%s",
                memory.metadata.trip_name,
                trip_id,
            )
        except AgentRunError as exc:
            logger.warning(
                "Trip description generation failed (non-fatal): %s  trip_id=%s",
                exc,
                trip_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Unexpected error in trip description generation: %s  trip_id=%s",
                exc,
                trip_id,
            )

    async def _regenerate_compressed_profile(
        self,
        user_id: int,
        full_text: str,
    ) -> None:
        """Rebuild the compressed profile after the full profile changes."""

        try:
            result = await self.agents.compression.run(
                f"Full travel profile:\n{full_text}"
            )
            self.repository.update_user_profile_compressed(
                user_id,
                result.compressed_text,
            )
        except AgentRunError as exc:
            logger.error("Profile compression failed: %s", exc)

    @staticmethod
    def _apply_profile_delta(
        current_full: str,
        delta: Any,
        *,
        expected_month: str,
    ) -> str:
        """Merge a structured profile delta into the current profile text.

        The delta uses evidence-tag format for new and updated items:
          "Transport: Trains preferred. [source=explicit, confidence=high, last_seen=2026-05]"

        Key-matching strips any legacy [Updated] prefix from existing lines so
        that old entries are transparently migrated to evidence-tag format when
        they are first updated.
        """

        lines = [line.strip() for line in current_full.splitlines() if line.strip()]
        for item in getattr(delta, "updated", []):
            clean_item = PostConfirmationService._validated_profile_item(
                item, expected_month
            )
            if clean_item is None:
                continue

            key = (
                clean_item.split(":", 1)[0].strip().lower()
                if ":" in clean_item
                else None
            )
            if key:
                filtered: list[str] = []
                for line in lines:
                    # Strip legacy [Updated] prefix before comparing keys.
                    normalized = line[10:] if line.startswith("[Updated] ") else line
                    existing_key = (
                        normalized.split(":", 1)[0].strip().lower()
                        if ":" in normalized
                        else None
                    )
                    if existing_key == key:
                        continue  # Drop old entry; new version is added below.
                    filtered.append(line)
                lines = filtered

            # Write the item as-is (with evidence tag) — no legacy [Updated] prefix.
            if clean_item not in lines:
                lines.append(clean_item)

        for item in getattr(delta, "new", []):
            clean_item = PostConfirmationService._validated_profile_item(
                item, expected_month
            )
            if clean_item is not None and clean_item not in lines:
                lines.append(clean_item)
        return "\n".join(lines)

    @staticmethod
    def _validated_profile_item(item: object, expected_month: str) -> str | None:
        """Accept only bounded, current, evidence-tagged durable facts."""

        if not isinstance(item, str):
            return None
        clean = item.strip()
        if not clean or len(clean) > 500 or "\n" in clean or ":" not in clean:
            return None

        match = _EVIDENCE_TAG.search(clean)
        if match is None or match.group(3) != expected_month:
            return None

        source, _, _, evidence_count = match.groups()
        if source == "observed" and (evidence_count is None or int(evidence_count) < 2):
            return None
        return clean
