"""Collect only source records actually referenced by one planner run."""

from __future__ import annotations

from typing import Any

from app.models.evidence import SourceRef
from app.providers.base import ProviderBundle


def collect_provider_sources(
    providers: ProviderBundle,
    used_source_ids: set[str],
) -> list[SourceRef]:
    registered: dict[str, SourceRef] = {}
    seen: set[int] = set()
    for provider in (
        providers.destination,
        providers.places,
        providers.routes,
        providers.weather,
        providers.currency,
        providers.current_facts,
    ):
        if provider is None:
            continue
        if id(provider) in seen:
            continue
        seen.add(id(provider))
        source_method = getattr(provider, "sources", None)
        if source_method is None:
            source_method = getattr(provider, "source_for_id", None)
            if source_method is not None:
                for source_id in used_source_ids:
                    source = source_method(source_id)
                    registered[source.id] = source
            continue
        for source in source_method():
            normalized = SourceRef.model_validate(_as_payload(source))
            registered[normalized.id] = normalized
    return [
        registered[source_id]
        for source_id in used_source_ids
        if source_id in registered
    ]


def _as_payload(value: Any) -> Any:
    return value.model_dump() if hasattr(value, "model_dump") else value
