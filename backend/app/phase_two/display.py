"""Explicit, bounded refresh of selected Places display facts; no durable cache."""

import copy

from app.planning_tools.contracts import Photo, Place, PlaceLookupInput


async def refresh_display(plan, catalog, *, place_ids=None, include_photos=False):
    result = copy.deepcopy(plan)
    events = [
        event for day in result.get("days", []) for event in day.get("events", [])
    ]
    selected = list(dict.fromkeys(e["place_id"] for e in events if e.get("place_id")))
    requested = selected[:10] if place_ids is None else list(dict.fromkeys(place_ids))
    if len(requested) > 10 or any(place_id not in selected for place_id in requested):
        raise ValueError("Refresh at most ten selected plan place IDs.")
    lookup, warnings = catalog.agent_tools()["places"], []
    refreshed = []
    for place_id in requested:
        response = await lookup(PlaceLookupInput(action="details", place_id=place_id))
        place = response.data
        if not isinstance(place, Place):
            warnings.append(f"Details unavailable for {place_id}.")
            continue
        refreshed.append(place_id)
        display_photos = []
        if include_photos and place.photos:
            ref = place.photos[0]
            image = await lookup(PlaceLookupInput(action="photo", photo_name=ref.name))
            if isinstance(image.data, Photo) and image.data.uri:
                display_photos = [
                    image.data.model_copy(
                        update={"attribution": ref.attribution}
                    ).model_dump(mode="json")
                ]
            else:
                warnings.append(f"Photo unavailable for {place_id}.")
        for event in events:
            if event.get("place_id") == place_id:
                event.update(
                    title=place.name,
                    reason=(event.get("reason") if event.get("reason") and not event["reason"].startswith("Refresh source details") else f"Planned {event.get('kind', 'activity')} stop at {place.name}."),
                    location=place.location.model_dump() if place.location else None,
                    address=place.address,
                    opening_hours=place.opening_hours,
                    map_url=place.map_url,
                    photos=[ref.model_dump() for ref in place.photos],
                    image_refs=place.photo_names,
                    display_photos=display_photos,
                )
    if len(selected) > len(requested):
        warnings.append(
            "Only the requested subset was refreshed; request another batch for remaining places."
        )
    warnings.append(
        "Display refreshed only; schedules and quoted prices were not revalidated. Show Google Maps and supplied author attribution."
    )
    return {
        "plan": result,
        "display_fields_status": "refreshed" if not warnings[:-1] else "partial",
        "warnings": warnings,
        "refreshed_place_ids": refreshed,
    }
