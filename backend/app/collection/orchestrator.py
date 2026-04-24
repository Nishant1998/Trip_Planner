"""Main collection-phase orchestrator for user trip conversations."""

from __future__ import annotations

import asyncio
import re
import logging
from datetime import date
from typing import Any

from pydantic import ValidationError

from app.exceptions import AgentRunError
from app.config import Settings
from app.models.api import (
    ConfirmationIntentOutput,
    ConversationAgentOutput,
    ExtractionOutput,
    PreClassifierOutput,
    ResponseOutput,
)
from app.models.enums import (
    FieldSource,
    LiveStatusPhase,
    TripStatus,
)
from app.db.tables import TripRow
from app.models.memory import TripMemory
from app.models.state import (
    build_extraction_current_values,
    serialize_agent_memory_view,
    serialize_runtime_memory,
)
from app.collection.defaults import apply_silent_defaults
from app.collection.prefill import apply_prefill_to_memory
from app.collection.readiness import (
    assess_collection_readiness,
    get_missing_required_fields,
)
from app.collection.geography import is_local_city_trip, normalize_currency_code
from app.collection.acceptance import (
    clear_resolved_mapping_ambiguity,
    remember_mapping_ambiguity,
    select_safe_extracted_fields,
)
from app.collection.confirmation_gate import (
    build_confirmation_summary,
    has_current_confirmation_summary,
    invalidate_confirmation_summary,
    mark_confirmation_summary_shown,
    normalize_budget_scope_notes,
)

from app.collection.dependencies import invalidate_dependents
from app.collection.advisories import collect_advisories
from app.collection.post_confirm.service import PostConfirmationService
from app.collection.pre_checks import (
    accept_pre_summary_checks,
    run_early_trip_sanity_checks,
    run_pre_summary_checks,
)
from app.collection.metadata import (
    make_temp_trip_title,
    should_update_temp_title,
)
from app.collection.state_machine import re_infer_derived_fields, transition
from app.collection.turn_io import (
    ExtractionAttempt,
    Publisher,
    TurnContext,
    TurnOutcome,
    build_turn_context,
    finalize_turn,
    load_memory_from_trip,
    load_trip_memory,
    publish_live_status,
)
from app.collection.prompts.confirmation import build_intent_task_prompt
from app.collection.prompts.extraction import build_extraction_task_prompt
from app.collection.prompts.conversation import build_conversation_task_prompt
from app.collection.prompts.classifier import build_pre_classifier_prompt
from app.collection.prompts.prefill import build_prefill_task_prompt
from app.db.repository import TripRepository
from app.services.llm_usage import capture_llm_usage
from app.services.agent_timing import capture_agent_timing

from app.collection.utils.sanitizer import (
    sanitize_user_facing_response as _sanitize_user_facing_response,
)
from app.collection.validation.rules import (
    build_quick_check_failure_payload,
    run_quick_checks,
)
from app.collection.validation.places import (
    build_place_check_failure_payload,
    run_place_check,
)
from app.collection.validation.invariants import run_invariant_checks

from app.collection.intelligence import maybe_build_destination_intelligence
from app.collection.travel_advice import TavilyTravelAdviceLookup, TravelAdviceLookup

logger = logging.getLogger(__name__)

# ===== Constants =====

TERMINAL_STATUS_MESSAGES = {
    TripStatus.PLANNING: (
        "Your trip is being planned now. I'll keep this moving in the background."
    ),
    TripStatus.CONFIRMED: (
        "This trip is already confirmed. If you want changes, tell me what to adjust."
    ),
    TripStatus.ARCHIVED: (
        "This trip is archived. We can start a fresh one whenever you like."
    ),
    TripStatus.CANCELLED: (
        "This trip is cancelled. We can start again with a new plan."
    ),
    TripStatus.FAILED: (
        "Something went wrong earlier. We can try again from the latest details."
    ),
}
DEFAULT_OPENING_MESSAGE = (
    "Hey! I'm Riya, your trip planning assistant. "
    "Tell me about the trip you have in mind - where, when, who's coming, "
    "and anything you've already decided. I'll take it from there."
)
REPAIR_SUFFIX = "\n\nReturn valid JSON only."
EXTRACTION_RETRY_MESSAGE = "I didn't quite catch that - could you rephrase?"
PRE_CLASSIFIER_LAST_BOT_FALLBACK = (
    "Riya is helping the user collect trip details for travel planning."
)
_SOURCE_PRIORITY: dict[FieldSource, int] = {
    FieldSource.DEFAULT: 0,
    FieldSource.INFERRED: 1,
    FieldSource.PROFILE: 2,
    FieldSource.USER: 3,
}
_ADVICE_REQUEST_PATTERN = re.compile(
    r"\b(?:recommend|suggest(?:ion)?s?|advice|ideas?|options?|help\s+(?:me\s+)?choose|what\s+(?:route|place|destination)\s+(?:should|would))\b",
    re.IGNORECASE,
)
TIER_A_FIELDS: list[str] = [
    "num_children",
    "travel_style",
    "long_distance_modes",
    "activity_interests",
    "dietary_restrictions",
]
TIER_B_FIELDS: list[tuple[str, Any]] = [
    (
        "hotel_tier",
        lambda m: (
            m.optional.spending_tier is not None
            and m.optional.spending_tier.value in ("mid_range", "premium", "luxury")
        ),
    ),
    (
        "accessibility_needs",
        lambda m: m.optional.num_seniors is not None and m.optional.num_seniors > 0,
    ),
]
_MERGED_LIST_FIELDS = {
    "activity_interests",
    "dietary_restrictions",
    "accessibility_needs",
    "avoid",
}

_EXPLICIT_CONFIRMATION_REPLIES = {
    "yes",
    "yes please",
    "proceed",
    "proceed anyway",
    "yep",
    "yeah",
    "confirm",
    "confirm trip",
    "approved",
    "looks good",
    "looks good to me",
    "go ahead",
    "start planning",
    "yes start planning",
    "yes start planning the trip",
}


def _is_explicit_confirmation(message: str) -> bool:
    """Accept concise, unambiguous approval without an avoidable model call."""

    normalized = re.sub(r"[^a-z0-9]+", " ", message.casefold()).strip()
    return normalized in _EXPLICIT_CONFIRMATION_REPLIES


_EXTRACTED_FIELD_ALIASES = {
    "depart_date": "start_date",
    "departure_date": "start_date",
    "arrival_date": "end_date",
    "return_date": "end_date",
}


def _merge_list_field_values(
    field_name: str,
    existing: list[Any],
    incoming: list[Any],
) -> list[Any]:
    """Keep explicit list requirements from being erased by partial follow-ups."""

    cleaned = list(incoming)
    if field_name == "dietary_restrictions":
        # "No allergies" answers a different question; it is not a diet and
        # must never overwrite vegetarian/vegan/Jain/etc.
        cleaned = [
            value
            for value in cleaned
            if str(value).casefold().replace("_", " ").strip()
            not in {"no allergies", "none", "no restriction", "no restrictions"}
        ]
    return list(dict.fromkeys([*existing, *cleaned]))


def _user_requested_advice(message: str) -> bool:
    """Recognize a small, language-level advice intent without place lists.

    The semantic pre-classifier remains the primary path.  This inexpensive
    fallback protects the common mixed turn where a traveller both gives facts
    and asks Riya to recommend a route.
    """

    return bool(_ADVICE_REQUEST_PATTERN.search(message))


class TripOrchestrator:
    """Coordinate the collection flow for one trip conversation."""

    def __init__(
        self,
        db: TripRepository,
        agents,
        advice_lookup: TravelAdviceLookup | None = None,
    ) -> None:
        self.db = db
        self.agents = agents
        self.post_confirmation = PostConfirmationService(db, agents)
        self.advice_lookup = advice_lookup or TavilyTravelAdviceLookup(
            Settings.from_env().tavily_api_key
        )
        self._post_confirmation_tasks: set[asyncio.Task[None]] = set()

    async def close(self) -> None:
        """Stop optional post-confirmation work before provider shutdown."""

        tasks = list(self._post_confirmation_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._post_confirmation_tasks.clear()
        await self.advice_lookup.close()

    async def bootstrap_trip(
        self, trip_id: int, publisher: Publisher | None = None
    ) -> ResponseOutput:
        """Initialize a trip while collecting all model usage in one DB write."""

        trip = self.db.get_trip(trip_id)
        if trip is None:
            raise ValueError(f"Trip {trip_id} not found")
        with (
            capture_llm_usage(
                self.db,
                user_id=trip.owner_id,
                trip_id=trip_id,
                phase="collection",
            ),
            capture_agent_timing(
                user_id=trip.owner_id,
                trip_id=trip_id,
                phase="collection",
            ),
        ):
            return await self._bootstrap_trip(trip_id, publisher)

    async def _bootstrap_trip(
        self, trip_id: int, publisher: Publisher | None = None
    ) -> ResponseOutput:
        """Initialize a trip and return the first assistant message."""

        trip, memory = self._load_trip_memory(trip_id)  # trip tabel row
        existing_messages = self.db.list_messages(trip_id)
        if memory.metadata.status != TripStatus.DRAFT and existing_messages:
            return existing_messages[0]["content"]  # asistent message

        if publisher is not None:
            await publisher.publish_status(
                LiveStatusPhase.BOOTSTRAPPING.value, "Applying saved preferences"
            )

        profile = self.db.get_user_profile_obj(trip.owner_id)
        compressed_text = profile.compressed_text if profile else ""
        opening = DEFAULT_OPENING_MESSAGE

        # Prefill belongs to new-trip bootstrap only.  Running an LLM call for
        # an empty profile adds latency without producing any useful defaults.
        if compressed_text.strip():
            try:
                prefill = await self.agents.prefill.run(
                    build_prefill_task_prompt(compressed_text)
                )
                apply_prefill_to_memory(memory, prefill)
            except AgentRunError as exc:
                logger.error("Prefill agent failed: %s", exc)

        previous_status = memory.metadata.status
        memory.metadata.status = transition(TripStatus.DRAFT, TripStatus.COLLECTING)
        self.db.save_trip_state(trip_id, serialize_runtime_memory(memory))
        if not existing_messages:
            self.db.insert_message(
                trip_id,
                "assistant",
                opening,
                meta={"branch": "bootstrap", "defaults_applied": [], "extracted": []},
            )
        if publisher is not None and previous_status != memory.metadata.status:
            await publisher.publish_state_change(
                previous_status.value, memory.metadata.status.value
            )
        return opening

    async def handle_message(
        self,
        trip_id: int,
        user_message: str,
        user_id: int,
        publisher: Publisher | None = None,
    ) -> TurnOutcome:
        """Handle a message and persist its model usage as one operation."""

        trip = self.db.get_trip(trip_id)
        if trip is None:
            raise ValueError(f"Trip {trip_id} not found")
        with (
            capture_llm_usage(
                self.db,
                user_id=user_id,
                trip_id=trip_id,
                phase="collection",
            ),
            capture_agent_timing(
                user_id=user_id,
                trip_id=trip_id,
                phase="collection",
            ),
        ):
            return await self._handle_message(
                trip_id,
                user_message,
                user_id,
                publisher,
            )

    async def _handle_message(
        self,
        trip_id: int,
        user_message: str,
        user_id: int,
        publisher: Publisher | None = None,
    ) -> TurnOutcome:
        """Handle one incoming user message based on the current trip state."""

        trip = self.db.get_trip(trip_id)  # single line from trip table
        if trip is None:
            raise ValueError(f"Trip {trip_id} not found")

        memory = self._load_memory_from_trip(trip)  # get the json
        if memory.metadata.status == TripStatus.DRAFT:  # trip is just created
            await self.bootstrap_trip(trip_id, publisher=publisher)
            trip = self.db.get_trip(trip_id) or trip
            memory = self._load_memory_from_trip(trip)

        if memory.metadata.status == TripStatus.AWAITING_CONFIRMATION:
            return await self._handle_confirmation_response(
                trip, memory, user_message, user_id, publisher
            )

        if memory.metadata.status == TripStatus.COLLECTING:
            return await self._main_loop(trip, memory, user_message, user_id, publisher)

        response = TERMINAL_STATUS_MESSAGES.get(
            memory.metadata.status,
            "Let's continue with your trip details.",
        )
        self.db.insert_message(
            trip_id,
            "assistant",
            response,
            meta={"branch": "terminal", "defaults_applied": [], "extracted": []},
        )
        return TurnOutcome(
            response_text=response, status=memory.metadata.status, branch="terminal"
        )

    async def _main_loop(
        self,
        trip: dict,
        memory: TripMemory,
        user_message: str,
        user_id: int,
        publisher: Publisher | None = None,
        suppress_confirmation_transition: bool = False,
    ) -> TurnOutcome:
        del user_id
        previous_status = memory.metadata.status

        # Emits entry and exit trace events for the main orchestration loop so the debug panel shows the full turn flow.
        await self._publish_trace(
            publisher,
            "orchestrator.main_loop",
            "enter",
            input_data={"user_message": user_message, "status": previous_status.value},
        )
        await self._publish_live_status(
            publisher,
            LiveStatusPhase.CLASSIFYING,
            "Understanding your message",
        )

        # Blank input has one certain route. Avoid a database history read and
        # an LLM classification call when there is nothing to classify.
        if not user_message.strip():
            outcome = TurnOutcome(
                response_text="I didn't catch that — could you type something?",
                status=memory.metadata.status,
                branch="empty_input",
            )
            return await self._finalize_turn(
                trip=trip,
                memory=memory,
                previous_status=previous_status,
                publisher=publisher,
                outcome=outcome,
            )

        turn_context = build_turn_context(self.db, trip, memory)
        last_bot_message = next(
            (
                line.partition(": ")[2]
                for line in reversed(turn_context.chat_history.splitlines())
                if line.startswith("assistant: ")
            ),
            PRE_CLASSIFIER_LAST_BOT_FALLBACK,
        )

        # Passes the pending_clarification state to the pre-classifier so user agreement with a proposed value routes to extraction.
        await self._publish_trace(
            publisher,
            "agent.pre_classifier",
            "enter",
            input_data={
                "last_assistant_message": last_bot_message,
                "user_message": user_message,
            },
        )
        pre_classifier = await self._run_pre_classifier(
            last_bot_message,
            user_message,
            pending_state=memory.internal.pending_clarification,
        )
        route = pre_classifier.route
        if pre_classifier.low_confidence:
            route = "run_extraction"
        advice_requested = pre_classifier.advice_requested or _user_requested_advice(
            user_message
        )
        await self._publish_trace(
            publisher,
            "agent.pre_classifier",
            "exit",
            output_data={
                "route": route,
                "low_confidence": pre_classifier.low_confidence,
                "advice_requested": advice_requested,
            },
        )

        logger.info(
            "Pre-classifier routed turn",
            extra={
                "trip_id": trip.id,
                "route": route,
                "model_route": pre_classifier.route,
                "low_confidence": pre_classifier.low_confidence,
                "advice_requested": advice_requested,
            },
        )

        extraction: ExtractionOutput | None = None
        changed_fields: list[str] = []
        failure_payload: list[dict[str, Any]] = []

        if route == "run_extraction":
            await self._publish_live_status(
                publisher,
                LiveStatusPhase.EXTRACTING,
                "Updating trip details",
            )
            await self._publish_trace(
                publisher,
                "agent.extraction",
                "enter",
                input_data={"user_message": user_message},
            )
            extraction_attempt = await self._run_extraction(
                user_message=user_message,
                chat_history=turn_context.chat_history,
                today=str(date.today()),
                current_values=build_extraction_current_values(memory),
            )
            if extraction_attempt.retries_exhausted:
                outcome = self._build_extraction_retry_outcome(memory)
                return await self._finalize_turn(
                    trip=trip,
                    memory=memory,
                    previous_status=previous_status,
                    publisher=publisher,
                    outcome=outcome,
                )

            extraction = extraction_attempt.output
            await self._publish_trace(
                publisher,
                "agent.extraction",
                "exit",
                output_data={
                    "fields": [
                        field.model_dump(mode="json")
                        for field in extraction.extracted_fields
                    ],
                    "is_correction": extraction.is_correction,
                },
            )

            await self._publish_live_status(
                publisher,
                LiveStatusPhase.VALIDATING,
                "Checking trip details",
            )
            await self._publish_trace(
                publisher,
                "orchestrator.validate_and_save",
                "enter",
                input_data={"field_names": extraction.field_names()},
            )
            accepted_fields, held_back_payload = select_safe_extracted_fields(
                extraction
            )
            remember_mapping_ambiguity(memory, extraction.mapping_ambiguity)
            safe_extraction = extraction.model_copy(
                update={"extracted_fields": accepted_fields}
            )
            (
                changed_fields,
                validation_failure_payload,
            ) = await self._validate_and_save_fields(
                memory,
                safe_extraction,
                publisher=publisher,
            )
            failure_payload = [*held_back_payload, *validation_failure_payload]
            failed_field_names = {
                str(item.get("field_name")) for item in validation_failure_payload
            }
            clear_resolved_mapping_ambiguity(
                memory,
                [
                    field
                    for field in accepted_fields
                    if field.field_name not in failed_field_names
                ],
            )
            await self._publish_trace(
                publisher,
                "orchestrator.validate_and_save",
                "exit",
                output_data={
                    "changed_fields": changed_fields,
                    "validation_failures": failure_payload,
                },
            )

            self._accumulate_extraction_signals(memory, extraction)

            # A model can occasionally miss the exact optional detail Riya
            # asked for, even when the user supplied it plainly. Retry once
            # with a narrow recovery hint instead of making the user repeat it.
            expected_field = self._last_unanswered_optional_field(memory)
            if (
                expected_field is not None
                and expected_field not in changed_fields
                and extraction.mapping_ambiguity is None
            ):
                recovery_attempt = await self._run_extraction(
                    user_message=user_message,
                    chat_history=turn_context.chat_history,
                    today=str(date.today()),
                    current_values=build_extraction_current_values(memory),
                    focus_field=expected_field,
                )
                if not recovery_attempt.retries_exhausted:
                    recovery = recovery_attempt.output
                    recovery_accepted, recovery_held_back = (
                        select_safe_extracted_fields(recovery)
                    )
                    recovery_safe = recovery.model_copy(
                        update={"extracted_fields": recovery_accepted}
                    )
                    (
                        recovery_changed,
                        recovery_failures,
                    ) = await self._validate_and_save_fields(
                        memory,
                        recovery_safe,
                        publisher=publisher,
                    )
                    changed_fields.extend(
                        field
                        for field in recovery_changed
                        if field not in changed_fields
                    )
                    failure_payload.extend([*recovery_held_back, *recovery_failures])
                    self._accumulate_extraction_signals(memory, recovery)
                    extraction = extraction.model_copy(
                        update={
                            "extracted_fields": [
                                *extraction.extracted_fields,
                                *recovery.extracted_fields,
                            ],
                            "profile_signals": [
                                *extraction.profile_signals,
                                *recovery.profile_signals,
                            ],
                            "trip_context_notes": [
                                *extraction.trip_context_notes,
                                *recovery.trip_context_notes,
                            ],
                        }
                    )

            # Set a Python temp title the moment the destination is known or changes.
            # No LLM call — just a template like "Trip to Goa". The real trip
            # description is generated post-confirmation by the metadata agent.
            if should_update_temp_title(memory, changed_fields):
                temp_title = make_temp_trip_title(memory)
                memory.metadata.trip_name = temp_title

        # Catch clear route and budget impossibilities as soon as the relevant
        # facts are known.  Optional preferences must never delay this warning.
        early_issues = run_early_trip_sanity_checks(memory)
        if early_issues and not advice_requested and not failure_payload:
            outcome = TurnOutcome(
                response_text="\n\n".join(issue.message for issue in early_issues),
                status=memory.metadata.status,
                branch="early_trip_sanity_gate",
                extracted_fields=extraction.field_names() if extraction else [],
                is_correction=extraction.is_correction if extraction else False,
            )
            return await self._finalize_turn(
                trip=trip,
                memory=memory,
                previous_status=previous_status,
                publisher=publisher,
                outcome=outcome,
            )

        # Answer questions and resolve rejected edits before offering approval.
        edit_applied = bool(
            changed_fields or (extraction and extraction.trip_context_notes)
        )
        needs_reply = bool(
            failure_payload
            or memory.internal.pending_clarification.active
            or advice_requested
            or (suppress_confirmation_transition and not edit_applied)
        )
        if not needs_reply:
            gate_outcome = await self._run_pre_summary_gate(
                trip=trip,
                memory=memory,
                previous_status=previous_status,
                route=route,
                extraction=extraction,
                changed_fields=changed_fields,
                publisher=publisher,
                user_message=user_message,
            )
            if gate_outcome is not None:
                return gate_outcome

        # Reaching the summary is a deterministic state, not a model decision.
        # This guarantees every ordinary user sees the same readable review
        # before they can confirm, without an extra "are you ready" message.
        phase, _, _ = self._determine_next_action(memory)
        if phase == "summary" and not needs_reply:
            defaults_applied = apply_silent_defaults(memory)
            memory.internal.enrichment_state.enrichment_complete = True
            memory.metadata.status = transition(
                TripStatus.COLLECTING,
                TripStatus.AWAITING_CONFIRMATION,
            )
            mark_confirmation_summary_shown(memory)
            outcome = TurnOutcome(
                response_text=build_confirmation_summary(memory),
                status=memory.metadata.status,
                branch="confirmation_ready",
                extracted_fields=extraction.field_names() if extraction else [],
                is_correction=extraction.is_correction if extraction else False,
                defaults_applied=defaults_applied,
            )
            return await self._finalize_turn(
                trip=trip,
                memory=memory,
                previous_status=previous_status,
                publisher=publisher,
                outcome=outcome,
            )

        await self._publish_trace(
            publisher,
            "agent.conversation",
            "enter",
            input_data={
                "changed_fields": changed_fields,
                "validation_failures": failure_payload,
            },
        )
        await self._publish_live_status(
            publisher,
            LiveStatusPhase.PREPARING_REPLY,
            "Preparing your response",
        )
        result = await self._run_conversation_agent(
            memory=memory,
            extraction=extraction,
            changed_fields=changed_fields,
            failure_payload=failure_payload,
            user_message=user_message,
            turn_context=turn_context,
            advice_requested=advice_requested,
        )
        await self._publish_trace(
            publisher,
            "agent.conversation",
            "exit",
            output_data={
                "response_text": result.response_text,
                "field_asked": result.field_asked,
                "ready_for_confirmation": result.ready_for_confirmation,
            },
        )

        # Guard: conversation agent returned an empty reply — replace with safe fallback.
        if not result.response_text or not result.response_text.strip():
            logger.warning(
                "Conversation agent returned empty response_text for trip %s; using fallback",
                trip.id,
            )
            result.response_text = "Got it — noted. What else would you like to add?"

        if result.field_asked:
            if result.field_asked not in memory.internal.enrichment_state.fields_asked:
                memory.internal.enrichment_state.fields_asked.append(result.field_asked)

        # Only transition to AWAITING_CONFIRMATION when the agent signals readiness
        # AND the caller has not asked us to stay in COLLECTING for this turn.
        # suppress_confirmation_transition=True is used when returning from a
        # confirmation-edit path so the status remains "collecting" for that turn.
        readiness = assess_collection_readiness(memory)
        allow_confirmation = (
            result.ready_for_confirmation
            and readiness.is_ready
            and not suppress_confirmation_transition
            and not needs_reply
        )
        if result.ready_for_confirmation and not readiness.is_ready:
            # Readiness is intentionally deterministic: the conversation model
            # may suggest a summary, but it cannot override essential facts.
            result.response_text = readiness.user_message()
        if allow_confirmation:
            # Defaults are part of the decision, so apply and display them
            # before the user is asked to approve the trip.
            apply_silent_defaults(memory)
            memory.internal.enrichment_state.enrichment_complete = True
            memory.metadata.status = transition(
                TripStatus.COLLECTING,
                TripStatus.AWAITING_CONFIRMATION,
            )
            mark_confirmation_summary_shown(memory)
            result.response_text = build_confirmation_summary(memory)

        outcome = TurnOutcome(
            response_text=result.response_text,
            status=memory.metadata.status,
            branch=(
                "confirmation_ready"
                if allow_confirmation
                else (
                    "collection_readiness_blocked"
                    if result.ready_for_confirmation and not readiness.is_ready
                    else "conversation"
                )
            ),
            extracted_fields=extraction.field_names() if extraction else [],
            is_correction=extraction.is_correction if extraction else False,
        )

        finalized = await self._finalize_turn(
            trip=trip,
            memory=memory,
            previous_status=previous_status,
            publisher=publisher,
            outcome=outcome,
        )
        await self._publish_trace(
            publisher,
            "orchestrator.main_loop",
            "exit",
            output_data={"branch": finalized.branch, "status": finalized.status.value},
        )
        return finalized

    async def _handle_confirmation_response(
        self,
        trip: dict,
        memory: TripMemory,
        user_message: str,
        user_id: int,
        publisher: Publisher | None = None,
    ) -> TurnOutcome:
        previous_status = memory.metadata.status
        last_summary = self._get_last_assistant_summary(trip.id)
        await self._publish_live_status(
            publisher,
            LiveStatusPhase.FINAL_CHECKS,
            "Reviewing your trip summary",
        )
        # The summary explicitly asks the traveller to reply "yes". Do not
        # spend a model call (or risk a model-classification failure) on an
        # unambiguous approval; reserve the intent agent for edits and nuance.
        intent = (
            ConfirmationIntentOutput(intent="confirm")
            if _is_explicit_confirmation(user_message)
            else await self._run_confirmation_intent(last_summary, user_message)
        )

        if intent.intent == "confirm":
            finalized = await self._confirm_trip(
                trip=trip,
                memory=memory,
                previous_status=previous_status,
                publisher=publisher,
            )
            if finalized.status == TripStatus.CONFIRMED:
                task = asyncio.create_task(self.post_confirmation.dispatch(trip.id))
                self._post_confirmation_tasks.add(task)
                task.add_done_callback(self._post_confirmation_tasks.discard)
            return finalized

        if intent.intent in {"change", "add"}:
            return await self._resume_collection_from_confirmation(
                trip=trip,
                memory=memory,
                previous_status=previous_status,
                user_message=intent.raw_change_text or user_message,
                user_id=user_id,
                publisher=publisher,
            )

        if intent.intent == "advice":
            return await self._answer_confirmation_advice(
                trip=trip,
                memory=memory,
                previous_status=previous_status,
                user_message=user_message,
                publisher=publisher,
            )

        memory.internal.intent_retry_count += 1
        outcome = TurnOutcome(
            response_text=(
                "Just checking — does the trip summary look right to go ahead with, or would you like to change something?"
                if memory.internal.intent_retry_count >= 2
                else "I want to make sure I understood — are you happy with the summary, or should I change something?"
            ),
            status=memory.metadata.status,
            branch="confirmation_unclear",
            checkpoint_payload={
                "kind": "confirmation",
                "message": last_summary,
                "pending": True,
            },
            checkpoint_type="confirmation",
        )
        return await self._finalize_turn(
            trip=trip,
            memory=memory,
            previous_status=previous_status,
            publisher=publisher,
            outcome=outcome,
        )

    async def _answer_confirmation_advice(
        self,
        *,
        trip: TripRow,
        memory: TripMemory,
        previous_status: TripStatus,
        user_message: str,
        publisher: Publisher | None,
    ) -> TurnOutcome:
        """Answer a trip question without discarding the pending approval."""

        await self._publish_live_status(
            publisher, LiveStatusPhase.PREPARING_REPLY, "Preparing travel advice"
        )
        current_advice_context = await self.advice_lookup.lookup(memory, user_message)
        turn_context = build_turn_context(self.db, trip, memory)
        result = await self._run_conversation_agent(
            memory=memory,
            extraction=None,
            changed_fields=[],
            failure_payload=[],
            user_message=user_message,
            turn_context=turn_context,
            advice_requested=True,
            current_advice_context=current_advice_context,
        )
        reminder = "Your trip summary is still ready—reply yes to start planning, or tell me what you’d like to change."
        response_text = result.response_text.strip()
        response_text = response_text or reminder
        return await self._finalize_turn(
            trip=trip,
            memory=memory,
            previous_status=previous_status,
            publisher=publisher,
            outcome=TurnOutcome(
                response_text=response_text,
                status=TripStatus.AWAITING_CONFIRMATION,
                branch="confirmation_advice",
                checkpoint_payload={
                    "kind": "confirmation",
                    "message": self._get_last_assistant_summary(trip.id),
                    "pending": True,
                },
                checkpoint_type="confirmation",
            ),
        )

    def _load_trip_memory(self, trip_id: int) -> tuple[TripRow, TripMemory]:
        """Load one trip row together with its parsed memory object."""

        trip, memory = load_trip_memory(self.db, trip_id)
        normalize_budget_scope_notes(memory)
        return trip, memory

    @staticmethod
    def _load_memory_from_trip(trip: TripRow) -> TripMemory:
        """Parse the serialized trip memory from one stored trip row."""

        return load_memory_from_trip(trip)

    async def _publish_live_status(
        self,
        publisher: Publisher | None,
        phase: LiveStatusPhase,
        message: str,
    ) -> None:
        """Publish one live status message when a publisher is available."""

        if publisher is not None:
            current = (phase.value, message)
            if getattr(publisher, "_riya_last_status", None) == current:
                return
            setattr(publisher, "_riya_last_status", current)
        await publish_live_status(publisher, phase.value, message)

    async def _publish_trace(
        self,
        publisher: Publisher | None,
        block: str,
        stage: str,
        *,
        input_data: Any = None,
        output_data: Any = None,
    ) -> None:
        """Forward structured runtime telemetry only when an SSE publisher exists."""

        if publisher is not None:
            await publisher.publish_trace(
                block,
                stage,
                input_data=input_data,
                output_data=output_data,
            )

    async def _run_pre_classifier(
        self,
        last_bot_message: str,
        user_message: str,
        pending_state: Any = None,
    ) -> PreClassifierOutput:
        """Run the lightweight route classifier and fail open to extraction."""

        try:
            return await self.agents.pre_classifier.run(
                build_pre_classifier_prompt(
                    last_bot_message, user_message, pending_state
                )
            )
        except AgentRunError as exc:
            logger.warning("Pre-classifier failed, defaulting to extraction: %s", exc)
            return PreClassifierOutput(route="run_extraction", low_confidence=False)

    def _get_last_assistant_message(self, trip_id: int) -> str:
        """Return the latest assistant message stored for the trip."""

        recent_messages = self.db.get_recent_messages(trip_id, limit=3)
        return next(
            (
                message.content
                for message in reversed(recent_messages)
                if message.role == "assistant"
            ),
            "",
        )

    def _get_last_assistant_summary(self, trip_id: int) -> str:
        """Return the latest assistant summary shown during confirmation."""

        recent_messages = self.db.get_recent_messages(trip_id, limit=30)
        for message in reversed(recent_messages):
            meta = getattr(message, "meta", {}) or {}
            if message.role == "assistant" and meta.get("branch") in {
                "confirmation_ready",
                "confirmation_summary_refreshed",
            }:
                return message.content
        return self._get_last_assistant_message(trip_id)

    async def _run_confirmation_intent(
        self,
        last_summary: str,
        user_message: str,
    ) -> ConfirmationIntentOutput:
        """Classify a confirmation reply using the intent agent."""

        try:
            return await self.agents.intent.run(
                build_intent_task_prompt(last_summary, user_message)
            )
        except (AgentRunError, ValidationError) as exc:
            logger.error("Intent agent failed: %s", exc)
            return ConfirmationIntentOutput(intent="unclear")

    async def _confirm_trip(
        self,
        trip: TripRow,
        memory: TripMemory,
        previous_status: TripStatus,
        publisher: Publisher | None,
    ) -> TurnOutcome:
        """Apply defaults and finalize the trip-detail collection trip-detail collection."""

        readiness = assess_collection_readiness(memory)
        pending = memory.internal.pending_clarification
        if not readiness.is_ready or pending.active:
            memory.metadata.status = transition(
                TripStatus.AWAITING_CONFIRMATION,
                TripStatus.COLLECTING,
            )
            outcome = TurnOutcome(
                response_text=(
                    pending.clarification_question
                    or "Please clarify the outstanding trip detail first."
                )
                if pending.active
                else readiness.user_message(),
                status=memory.metadata.status,
                branch="confirmation_blocked_by_readiness",
            )
            return await self._finalize_turn(
                trip=trip,
                memory=memory,
                previous_status=previous_status,
                publisher=publisher,
                outcome=outcome,
                clear_checkpoint=True,
            )

        if not has_current_confirmation_summary(memory):
            mark_confirmation_summary_shown(memory)
            outcome = TurnOutcome(
                response_text=build_confirmation_summary(memory),
                status=memory.metadata.status,
                branch="confirmation_summary_refreshed",
            )
            return await self._finalize_turn(
                trip=trip,
                memory=memory,
                previous_status=previous_status,
                publisher=publisher,
                outcome=outcome,
            )

        memory.metadata.status = transition(
            TripStatus.AWAITING_CONFIRMATION,
            TripStatus.CONFIRMED,
        )
        await self._publish_live_status(
            publisher,
            LiveStatusPhase.FINAL_CHECKS,
            "Saving your confirmed trip details",
        )
        outcome = TurnOutcome(
            response_text=("Perfect! Your trip details are confirmed and saved."),
            status=memory.metadata.status,
            branch="confirm",
        )
        return await self._finalize_turn(
            trip=trip,
            memory=memory,
            previous_status=previous_status,
            publisher=publisher,
            outcome=outcome,
            clear_checkpoint=True,
        )

    async def _resume_collection_from_confirmation(
        self,
        trip: TripRow,
        memory: TripMemory,
        previous_status: TripStatus,
        user_message: str,
        user_id: int,
        publisher: Publisher | None,
    ) -> TurnOutcome:
        """Return from confirmation to collection when the user wants changes."""

        memory.metadata.status = transition(
            TripStatus.AWAITING_CONFIRMATION,
            TripStatus.COLLECTING,
        )
        invalidate_confirmation_summary(memory)
        self.db.save_trip_state(trip.id, serialize_runtime_memory(memory))
        self.db.clear_pending_checkpoints(trip.id)

        if publisher is not None:
            await publisher.publish_state_change(
                previous_status.value,
                memory.metadata.status.value,
            )

        refreshed_trip = self.db.get_trip(trip.id) or trip
        return await self._main_loop(
            trip=refreshed_trip,
            memory=memory,
            user_message=user_message,
            user_id=user_id,
            publisher=publisher,
            suppress_confirmation_transition=True,
        )

    async def _finalize_turn(
        self,
        trip: TripRow,
        memory: TripMemory,
        previous_status: TripStatus,
        publisher: Publisher | None,
        outcome: TurnOutcome,
        clear_checkpoint: bool = False,
    ) -> TurnOutcome:
        return await finalize_turn(
            self.db,
            trip,
            memory,
            previous_status,
            publisher,
            outcome,
            clear_checkpoint=clear_checkpoint,
        )

    async def _validate_and_save_fields(
        self,
        memory: TripMemory,
        extraction: ExtractionOutput,
        publisher: Publisher | None = None,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """
        Validate extracted fields then write the valid ones to memory.

        Returns
        -------
        changed_fields
            Field names that were actually written to memory this turn.
        failure_payload
            Merged list of quick-check and place-check failures,
            ready to pass to the response agent.
        """

        # Normalize the small set of known extractor variations before any
        # validation or place checks. This keeps user-supplied places such as
        # "Goa" and date aliases from being rejected by schema-only checks.
        normalized_fields = []
        for extracted_field in extraction.extracted_fields:
            field_name = _EXTRACTED_FIELD_ALIASES.get(
                extracted_field.field_name,
                extracted_field.field_name,
            )
            value = extracted_field.value

            if field_name == "trip_context_notes":
                note_values = value if isinstance(value, list) else [value]
                for note in note_values:
                    cleaned_note = str(note).strip()
                    if (
                        cleaned_note
                        and cleaned_note not in memory.internal.trip_context_notes
                    ):
                        memory.internal.trip_context_notes.append(cleaned_note)
                continue

            if field_name == "destination":
                if isinstance(value, str) and value.strip():
                    value = {
                        "type": "city",
                        "items": [value.strip()],
                        "subtype": None,
                    }
                elif isinstance(value, list) and value:
                    items = [str(item).strip() for item in value if str(item).strip()]
                    value = {
                        "type": "cities" if len(items) > 1 else "city",
                        "items": items,
                        "subtype": None,
                    }

            normalized_fields.append(
                extracted_field.model_copy(
                    update={"field_name": field_name, "value": value}
                )
            )
        extraction = extraction.model_copy(
            update={"extracted_fields": normalized_fields}
        )

        # ── Validation: validate ─────────────────────────────────────────────

        # Runs invariant checks and logs warnings without blocking field writes, so date inconsistencies are visible in logs before recomputation.
        invariant_warnings = run_invariant_checks(extraction, memory)
        for warning in invariant_warnings:
            # Derived fields are recomputed below.  Keep this diagnostic out of
            # normal terminal/UI output; it is useful only while debugging.
            logger.debug("Invariant check: %s", warning)

        quick_failures, place_check_fields = run_quick_checks(
            extraction.extracted_fields,
        )
        failed_field_names = {f.field_name for f in quick_failures}

        place_check_envelope = await run_place_check(
            extraction.extracted_fields,
            place_check_fields,
            getattr(self.agents, "place_verifier", None),
        )

        place_failures = build_place_check_failure_payload(place_check_envelope)
        for issue in place_check_envelope.issues:
            if issue.action == "reject":
                failed_field_names.add(issue.field_name)

        destination_verified_this_turn = any(
            issue.field_name == "destination"
            and issue.action == "pass"
            and issue.reason_code == "verified_place"
            for issue in place_check_envelope.issues
        )

        # Merge quick-check and place-check failures into one payload.
        quick_payload = build_quick_check_failure_payload(quick_failures)
        failure_payload: list[dict[str, Any]] = [
            *[
                {
                    "field_name": item["field_name"],
                    "candidate_value": item["rejected_value"],
                    "reason": item["reason"],
                    "reason_code": "quick_check_failed",
                }
                for item in quick_payload
            ],
            *place_failures,
        ]

        # ── Persistence: write valid fields to memory ─────────────────────────

        changed_fields: list[str] = []

        for extracted_field in extraction.extracted_fields:
            field_name = extracted_field.field_name
            if field_name in failed_field_names:
                continue

            # The LLM owns duration interpretation.  We only retain its exact
            # user-facing duration phrase for the confirmation summary; no
            # Python conversion from nights to days occurs here.
            if field_name == "trip_duration_days":
                raw_duration = (extracted_field.raw_text or "").strip()
                memory.internal.duration_display_label = (
                    raw_duration if "night" in raw_duration.casefold() else None
                )

            stored = memory.get_variable_record(field_name)
            new_value = extracted_field.value

            # A partial follow-up such as "no allergies" must not erase an
            # already stated dietary requirement such as vegetarian. List facts
            # accumulate unless the user clearly says they are correcting them.
            if (
                field_name in _MERGED_LIST_FIELDS
                and isinstance(stored.value if stored else None, list)
                and isinstance(new_value, list)
                and not extracted_field.is_correction
            ):
                new_value = _merge_list_field_values(
                    field_name, stored.value, new_value
                )

            try:
                if stored is None:
                    # Field not yet in memory — first safe write.
                    memory.set_field(field_name, new_value, extracted_field.source)
                    changed_fields.append(field_name)

                elif stored.value == new_value:
                    # Value unchanged — nothing to do.
                    continue

                else:
                    # User-set values cannot be overwritten by non-user sources.
                    if (
                        stored.source == FieldSource.USER
                        and extracted_field.source != FieldSource.USER
                    ):
                        logger.info(
                            "Skipping non-user overwrite for user-set field %s",
                            field_name,
                        )
                        continue

                    # Allow overwrite only when the new value comes from the user,
                    # is an explicit correction, or has higher source priority.
                    should_overwrite = (
                        extracted_field.source == FieldSource.USER
                        or extracted_field.is_correction
                        or _SOURCE_PRIORITY.get(extracted_field.source, -1)
                        > _SOURCE_PRIORITY.get(stored.source, -1)
                    )
                    if not should_overwrite:
                        logger.info(
                            "Skipping lower-priority overwrite for field %s",
                            field_name,
                        )
                        continue

                    memory.set_field(field_name, new_value, extracted_field.source)
                    if extracted_field.source == FieldSource.USER:
                        memory.internal.correction_count += 1
                    changed_fields.append(field_name)

            except (AttributeError, ValidationError, ValueError, TypeError) as exc:
                logger.warning(
                    "Skipping invalid field %s value %r: %s",
                    field_name,
                    new_value,
                    exc,
                )
                continue

        # The extractor already separates a stated currency from its numeric
        # budget. Preserve that explicit user choice rather than assuming a
        # country-specific currency from the route.
        for extracted_field in extraction.extracted_fields:
            if (
                extracted_field.field_name != "trip_budget"
                or extracted_field.field_name in failed_field_names
                or extracted_field.source != FieldSource.USER
            ):
                continue
            currency_code = normalize_currency_code(extracted_field.currency_mentioned)
            if currency_code is not None:
                memory.metadata.currency_code = currency_code

        # ── Phase 3: recalculate derived fields ───────────────────────────

        # Invalidates stale inferred dependents on field change, rebuilds destination intelligence when endpoints or date change, and infers num_children=0 for senior-only parties.
        if changed_fields:
            cleared = invalidate_dependents(memory, set(changed_fields))
            if cleared:
                logger.info("Cleared stale dependent fields: %s", cleared)

        if destination_verified_this_turn:
            memory.internal.sanity_state.destination_verified = True
            destination = memory.needed.destination
            if destination and destination.type in {"city", "cities", "route", "poi"}:
                memory.internal.sanity_state.resolved_city_names = list(
                    destination.items
                )

        # Rebuild the one destination-research record when any research input changes.
        research_fields = {
            "origin",
            "destination",
            "start_date",
            "end_date",
            "long_distance_modes",
        }
        if research_fields.intersection(changed_fields):
            memory.internal.sanity_state.destination_intelligence = None
            memory.internal.sanity_state.destination_intelligence_snapshot = None
            destination = memory.needed.destination
            can_research_destination = bool(
                memory.needed.origin
                and destination
                and destination.items
                and getattr(self.agents, "destination_intelligence", None) is not None
            )
            if can_research_destination:
                await self._publish_live_status(
                    publisher,
                    LiveStatusPhase.ENRICHING,
                    "Checking destination details",
                )
            await maybe_build_destination_intelligence(
                memory,
                getattr(self, "agents", None),
            )

        if (
            memory.optional.num_children is None
            and memory.needed.num_adults is not None
        ):
            memory.set_field("num_children", 0, FieldSource.INFERRED)

        re_infer_derived_fields(memory)

        if is_local_city_trip(memory):
            failure_payload = [
                issue for issue in failure_payload
                if issue.get('field_name') != 'long_distance_modes'
            ]

        return changed_fields, failure_payload

    @staticmethod
    def _accumulate_extraction_signals(
        memory: TripMemory,
        extraction: ExtractionOutput,
    ) -> None:
        """Append new profile signals and trip context notes to memory.

        These accumulate turn by turn during collection. After the user
        confirms, they are compressed into paragraphs for the planning
        pipeline.
        """
        for signal in extraction.profile_signals or []:
            if signal not in memory.internal.profile_signals:
                memory.internal.profile_signals.append(signal)
        for note in extraction.trip_context_notes or []:
            if note not in memory.internal.trip_context_notes:
                memory.internal.trip_context_notes.append(note)
        normalize_budget_scope_notes(memory)

    @staticmethod
    def _get_next_tier_a_field(memory: TripMemory) -> str | None:
        """Return the next Tier A optional field to ask, or None if all done."""

        for field in TIER_A_FIELDS:
            if field == "long_distance_modes" and is_local_city_trip(memory):
                continue
            if field == "num_children":
                # Infers num_children=0 automatically for solo travelers and senior-only parties so Riya does not ask redundant child questions.
                if memory.optional.num_children is not None:
                    continue
                if memory.needed.num_adults is not None:
                    memory.set_field("num_children", 0, FieldSource.INFERRED)
                    continue
                if (
                    memory.optional.num_seniors is not None
                    and memory.optional.num_seniors > 0
                ):
                    memory.set_field("num_children", 0, FieldSource.INFERRED)
                    continue
            elif memory.get_field_value(field) is not None:
                continue

            return field

        return None

    @staticmethod
    def _get_next_tier_b_field(memory: TripMemory) -> str | None:
        """Return the next eligible Tier B optional field, or None if all done."""

        for field, condition in TIER_B_FIELDS:
            if memory.get_field_value(field) is not None:
                continue
            try:
                if not condition(memory):
                    continue
            except Exception:
                continue
            return field

        return None

    @staticmethod
    def _determine_next_action(
        memory: TripMemory,
    ) -> tuple[str, str | None, list[str]]:
        """Decide what Riya should do this turn."""

        missing_required = get_missing_required_fields(memory)
        if missing_required:
            return "required", missing_required[0], missing_required

        next_field = TripOrchestrator._get_next_tier_a_field(memory)
        if next_field is not None:
            return "tier_a", next_field, []

        next_field = TripOrchestrator._get_next_tier_b_field(memory)
        if next_field is not None:
            return "tier_b", next_field, []

        return "summary", None, []

    async def _run_pre_summary_gate(
        self,
        *,
        trip,
        memory: TripMemory,
        previous_status,
        route: str,
        extraction,
        changed_fields: list[str],
        publisher,
        user_message: str,
    ) -> TurnOutcome | None:
        """Run the pre-summary checks and handle gate logic.

        Returns a TurnOutcome if the gate fires (blocking the summary for this
        turn), or None if checks pass and _main_loop should continue normally.
        """

        # Only relevant when we are about to generate the summary.
        phase, _, _ = self._determine_next_action(memory)
        if phase != "summary":
            return None

        gate = memory.internal.sanity_state.pre_summary_gate

        # Run all three checks.
        await self._publish_live_status(
            publisher, LiveStatusPhase.VALIDATING, "Checking trip details"
        )
        await self._publish_trace(
            publisher,
            "orchestrator.pre_summary_gate",
            "enter",
            input_data={"phase": phase},
        )
        result = await run_pre_summary_checks(memory, self.agents)
        if (
            gate.gate_awaiting_response
            and not changed_fields
            and _is_explicit_confirmation(user_message)
        ):
            accept_pre_summary_checks(memory, result)
            result.issues = [
                issue for issue in result.issues if issue.check_name != "budget"
            ]
        await self._publish_trace(
            publisher,
            "orchestrator.pre_summary_gate",
            "exit",
            output_data={
                "issues": [
                    {"check": i.check_name, "blocks": i.blocks_summary}
                    for i in result.issues
                ],
            },
        )

        if not result.has_blocking_issues:
            # The confirmation summary also displays the booking warning.
            gate.gate_awaiting_response = False
            return None

        # Blocking issue(s) found — surface them as one combined message and
        # stay in COLLECTING until the user responds.
        gate.gate_awaiting_response = True
        response_text = result.combined_user_message(proceed_hint=True)

        outcome = TurnOutcome(
            response_text=response_text,
            status=memory.metadata.status,  # stays COLLECTING
            branch="pre_summary_gate",
            extracted_fields=extraction.field_names() if extraction else [],
            is_correction=extraction.is_correction if extraction else False,
        )
        return await self._finalize_turn(
            trip=trip,
            memory=memory,
            previous_status=previous_status,
            publisher=publisher,
            outcome=outcome,
        )

    async def _run_conversation_agent(
        self,
        *,
        memory: TripMemory,
        extraction: ExtractionOutput | None,
        changed_fields: list[str],
        failure_payload: list[dict[str, Any]],
        user_message: str,
        turn_context: TurnContext,
        advice_requested: bool = False,
        current_advice_context: list[str] | None = None,
    ) -> ConversationAgentOutput:
        """Preprocess context, build the task prompt, and run the conversation agent."""

        phase, next_field, missing_required = self._determine_next_action(memory)

        next_field_schema: dict | None = None
        if next_field is not None:
            all_field_schemas = build_extraction_current_values(memory)
            next_field_schema = all_field_schemas.get(next_field)

        advisories = collect_advisories(memory)

        # Fold any booking-window note into the summary advisories so Riya
        # includes it naturally when generating the confirmation summary.
        booking_note = memory.internal.sanity_state.pre_summary_gate.booking_window_note
        if booking_note and phase == "summary":
            advisories = list(advisories) + [booking_note]

        if advice_requested and current_advice_context is None:
            current_advice_context = await self.advice_lookup.lookup(
                memory, user_message
            )

        ambiguity = (
            extraction.mapping_ambiguity.model_dump()
            if extraction and extraction.mapping_ambiguity
            else None
        )
        pending = memory.internal.pending_clarification
        if ambiguity is None and pending.active:
            ambiguity = {
                "question": pending.clarification_question,
                "candidate_fields": pending.options,
            }

        prompt = build_conversation_task_prompt(
            phase=phase,
            next_field=next_field,
            next_field_schema=next_field_schema,
            missing_required=missing_required,
            changed_fields=changed_fields,
            failure_payload=failure_payload,
            mapping_ambiguity=ambiguity,
            is_correction=extraction.is_correction if extraction else False,
            memory_json=serialize_agent_memory_view(memory, "collection"),
            user_message=user_message,
            chat_history=turn_context.chat_history,
            compressed_profile=turn_context.compressed_profile,
            traveler_context=turn_context.traveler_context,
            advisories=advisories,
            destination_intelligence=memory.internal.sanity_state.destination_intelligence,
            trip_context_notes=memory.internal.trip_context_notes,
            advice_requested=advice_requested,
            current_advice_context=current_advice_context,
        )

        for attempt in range(2):
            try:
                if attempt == 1:
                    prompt += REPAIR_SUFFIX
                # Readiness checks can replace this reply with the confirmation
                # summary. The runtime publishes only the final, saved message.
                output = await self.agents.conversation.run(prompt)
                # Apply the terminology guard after every conversation-agent call.
                output.response_text = _sanitize_user_facing_response(
                    output.response_text
                )
                return output
            except AgentRunError as exc:
                if attempt == 1:
                    logger.error("Conversation agent failed twice: %s", exc)

        return ConversationAgentOutput(
            response_text="I'm having a moment — could you rephrase that?",
            ready_for_confirmation=False,
            field_asked=None,
        )

    async def _run_extraction(
        self,
        user_message: str,
        chat_history: str,
        today: str,
        current_values: dict[str, Any],
        focus_field: str | None = None,
    ) -> ExtractionAttempt:
        for attempt in range(2):
            try:
                prompt = build_extraction_task_prompt(
                    user_message=user_message,
                    today=today,
                    chat_history=chat_history,
                    current_values=current_values,
                    focus_field=focus_field,
                )
                if attempt == 1:
                    prompt += REPAIR_SUFFIX
                return ExtractionAttempt(
                    output=await self.agents.extraction.run(prompt)
                )
            except AgentRunError as exc:
                if attempt == 1:
                    logger.error("Extraction agent failed twice: %s", exc)
                    return ExtractionAttempt(
                        output=ExtractionOutput(),
                        retries_exhausted=True,
                    )
        return ExtractionAttempt(output=ExtractionOutput(), retries_exhausted=True)

    @staticmethod
    def _last_unanswered_optional_field(memory: TripMemory) -> str | None:
        """Return the latest optional prompt whose value is still absent."""

        optional_fields = {
            *TIER_A_FIELDS,
            *(field for field, _ in TIER_B_FIELDS),
        }
        for field in reversed(memory.internal.enrichment_state.fields_asked):
            if field in optional_fields and memory.get_field_value(field) is None:
                return field
        return None

    @staticmethod
    def _build_extraction_retry_outcome(memory: TripMemory) -> TurnOutcome:
        """Build the visible retry outcome after repeated extraction failure."""

        return TurnOutcome(
            response_text=EXTRACTION_RETRY_MESSAGE,
            status=memory.metadata.status,
            branch="extraction_retry",
            error_message=EXTRACTION_RETRY_MESSAGE,
        )
