"""Keep the agent's POI shortlist inside the exact evidence catalog."""

from __future__ import annotations

from math import ceil

from app.models.agent import ResearchCompletion
from app.models.evidence import EvidenceBundle, PlaceEvidence
from app.models.context import PlanningContext, ResolvedDestination


def normalize_place_shortlist(
    context: PlanningContext,
    destination: ResolvedDestination,
    evidence: EvidenceBundle,
    completion: ResearchCompletion,
) -> ResearchCompletion:
    """Drop invalid IDs without silently substituting unrelated places.

    Missing selections are intentionally left for the evidence gate to report.
    The research agent then gets one targeted retry with those exact shortages.
    This is safer than changing the meaning of a trip through a provider-ranked
    fallback that does not understand why the traveller is visiting a stop.
    """

    usable = [place for place in evidence.places if _usable(place)]
    place_map = {place.id: place for place in usable}
    maximum = maximum_places_per_stop(context, len(destination.stops))
    selected: list[str] = []
    for stop in destination.stops:
        model_chosen = list(
            dict.fromkeys(
                place_id
                for place_id in completion.selected_place_ids
                if place_id in place_map
                and place_map[place_id].stop_name.casefold() == stop.name.casefold()
            )
        )
        primary = [
            place.id
            for place in usable
            if place.stop_name.casefold() == stop.name.casefold()
            and place.is_primary_destination
        ]
        chosen = list(dict.fromkeys([*primary, *model_chosen]))[:maximum]
        selected.extend(chosen)

    gaps = list(completion.unresolved_gaps)
    rejected = [
        place_id
        for place_id in dict.fromkeys(completion.selected_place_ids)
        if place_id not in selected
    ]
    if rejected:
        gaps.append(
            "Rejected selected_place_ids that were not exact selectable_place_id "
            "values for their resolved stops: " + ", ".join(rejected)
        )
    return completion.model_copy(
        update={
            "selected_place_ids": selected,
            "unresolved_gaps": list(dict.fromkeys(gaps)),
        }
    )


def minimum_places_per_stop(context: PlanningContext, stop_count: int) -> int:
    if stop_count >= context.duration_days:
        return 1
    days_per_stop = ceil(context.duration_days / max(1, stop_count))
    return min(4, max(2, days_per_stop))


def maximum_places_per_stop(context: PlanningContext, stop_count: int) -> int:
    days_per_stop = ceil(context.duration_days / max(1, stop_count))
    return max(minimum_places_per_stop(context, stop_count), min(6, days_per_stop * 2))


def _usable(place: PlaceEvidence) -> bool:
    # Opening periods are valuable scheduling evidence, but their absence does
    # not erase a real, source-backed place.  The planner treats that time as
    # provisional and emits a traveller-facing confirmation warning.
    return bool(place.provider_place_id and place.source_ids)
