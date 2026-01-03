"""Typed agent registry for the collection-phase workflow."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module

from app.collection.agents.base import AgentInitializationError, SupportsRun
from app.config import Settings
from app.models.api import (
    ConfirmationIntentOutput,
    ConversationAgentOutput,
    DestinationIntelligenceOutput,
    ExtractionOutput,
    PreClassifierOutput,
    PrefillOutput,
    ProfileUpdateOutput,
    TripFeasibilityEstimate,
    TripMetadataOutput,
)
from app.models.user import CompressedProfileOutput
from app.providers.openroute import OpenRouteServiceProvider

_BUILDER_MODULE = "app.collection.agents.builders"
AGENT_BUILDERS = {
    name: f"build_{name}_agent"
    for name in (
        "prefill",
        "pre_classifier",
        "extraction",
        "conversation",
        "destination_intelligence",
        "feasibility",
        "intent",
        "profile_update",
        "metadata",
        "compression",
    )
}


# ===== Core Logic =====


def _build_agent(agent_name: str):
    """Build one agent runner and wrap initialization failures clearly."""

    builder_name = AGENT_BUILDERS[agent_name]
    try:
        module = import_module(_BUILDER_MODULE)
        builder = getattr(module, builder_name)
        return builder()
    except (ImportError, AttributeError, TypeError, ValueError, RuntimeError) as exc:
        raise AgentInitializationError(agent_name, exc) from exc


@dataclass(slots=True)
class AgentRegistry:
    """Container for every agent runner used by the app runtime."""

    prefill: SupportsRun[PrefillOutput]
    pre_classifier: SupportsRun[PreClassifierOutput]
    extraction: SupportsRun[ExtractionOutput]
    conversation: SupportsRun[ConversationAgentOutput]
    destination_intelligence: SupportsRun[DestinationIntelligenceOutput]
    # Pre-summary gate.
    feasibility: SupportsRun[TripFeasibilityEstimate]
    # Confirmation phase.
    intent: SupportsRun[ConfirmationIntentOutput]
    # Post-confirmation agents keep independent persistence/failure boundaries.
    profile_update: SupportsRun[ProfileUpdateOutput]
    metadata: SupportsRun[TripMetadataOutput]
    # Profile maintenance.
    compression: SupportsRun[CompressedProfileOutput]
    place_verifier: OpenRouteServiceProvider | None = None

    @classmethod
    def build_default(cls) -> AgentRegistry:
        """Build the default set of agents used by the app runtime."""

        settings = Settings.from_env()

        return cls(
            prefill=_build_agent("prefill"),
            pre_classifier=_build_agent("pre_classifier"),
            extraction=_build_agent("extraction"),
            conversation=_build_agent("conversation"),
            destination_intelligence=_build_agent("destination_intelligence"),
            feasibility=_build_agent("feasibility"),
            intent=_build_agent("intent"),
            profile_update=_build_agent("profile_update"),
            metadata=_build_agent("metadata"),
            compression=_build_agent("compression"),
            place_verifier=(
                OpenRouteServiceProvider(
                    api_key=settings.ors_api_key,
                    timeout_seconds=min(settings.timeout_seconds, 20),
                )
                if settings.ors_api_key
                else None
            ),
        )

    async def close(self) -> None:
        """Close the shared place-verification HTTP client, when configured."""

        if self.place_verifier is not None:
            await self.place_verifier.close()
