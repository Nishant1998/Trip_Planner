"""Project saved research facts into dated utility inputs without new research."""
from datetime import date, datetime, time, timedelta

from ..tools.scheduling import opening_bounds, opening_status
from .bindings import PlannerUtilities
from .calculation import CostItem
from .contracts import fingerprint
from .timeline import TimelineStep, Window
from .currency import saved_rates


def prepare_utilities(data, *, routes, web):
    entities = {stop.id for stop in data.stops} | {poi.id for poi in data.pois} | {"origin_anchor", "end_anchor"}
    if routes:
        entities.update(routes.resolver.nodes)
    steps, costs = {}, {}
    evidence = {item["id"]: item for item in data.evidence if item.get("id")}
    for poi in data.pois:
        duration = poi.visit_minutes or poi.recommended_visit_minutes
        references = [key for key in poi.evidence_ids if key in evidence]
        for offset in range(data.duration_days):
            day = date.fromisoformat(data.start_date) + timedelta(days=offset)
            midnight = datetime.combine(day, time())
            try:
                bounds = opening_bounds(poi, day.isoformat())
                closed = opening_status(poi, day.isoformat(), 0, 1)[0] == "closed"
            except (ValueError, TypeError):
                bounds, closed = [], False
            step = TimelineStep(id=f"visit:{poi.id}:{day.isoformat()}", kind="visit",
                origin_id=poi.id, destination_id=poi.id, minutes=duration,
                minimum_minutes=poi.minimum_visit_minutes,
                basis="estimate" if duration is not None else "unknown",
                windows=[Window(start=midnight + timedelta(minutes=left), end=midnight + timedelta(minutes=right))
                         for left, right in bounds], availability_known=bool(bounds) or closed,
                session_required=poi.reservation_required is True, session_confirmed=False)
            steps[step.id] = step.model_dump(mode="json")
            steps[step.id]["applicable_date"] = day.isoformat()
        adults = data.travellers.get("num_adults")
        known_adults = isinstance(adults, int) and not isinstance(adults, bool) and adults > 0
        known_price = poi.adult_entry_cost is not None and bool(poi.entry_currency)
        item = CostItem(id=f"adult_entry:{poi.id}", category="activities",
            lower=str(poi.adult_entry_cost) if known_adults and known_price else None,
            upper=None, quantity=adults if known_adults else 1, unit="person", currency=poi.entry_currency or data.currency,
            basis="estimate" if known_adults and known_price else "unknown", evidence_ids=references,
            note="Adult admission for the confirmed adult count only; child/senior concessions and future prices remain unconfirmed."
                 if known_adults else "Adult count is unknown; no party admission subtotal can be established.")
        costs[item.id] = item.model_dump(mode="json")
        children = data.travellers.get("num_children", 0)
        if isinstance(children, int) and not isinstance(children, bool) and children > 0:
            item = CostItem(id=f"child_entry:{poi.id}", category="activities",
                quantity=children, unit="person", currency=poi.entry_currency or data.currency,
                basis="unknown", note="Child admission and age-specific concessions have not been established.")
            costs[item.id] = item.model_dump(mode="json")
        if web:
            fields = []
            if duration is None:
                fields.append(("visit_duration", "recommended visit duration"))
            if not poi.opening_periods and not poi.opening_hours:
                fields.append(("opening_hours", "official opening hours"))
            if poi.adult_entry_cost is None or (isinstance(children, int) and not isinstance(children, bool) and children > 0):
                fields.append(("admission", "official admission prices"))
            for field, label in fields:
                key = fingerprint({"entity": poi.id, "field": field, "dates": [data.start_date, data.end_date]})[:24]
                web.gaps[key] = {"entity_id": poi.id, "field": field,
                    "query": f"{poi.name} {label} {data.start_date} {data.end_date}", "resolved": False}
                for reference in references:
                    url = evidence[reference].get("url")
                    if url:
                        try:
                            web.registry.register(entity_id=poi.id, field=field, url=url, title=poi.name, basis="research_source")
                        except ValueError:
                            # Unsafe source is not usable for retrieval; the gap remains.
                            continue
    if web:
        names = {stop.id: stop.name for stop in data.stops}
        names.update({poi.id: poi.name for poi in data.pois})
        if routes:
            names.update({key: node.name for key, node in routes.resolver.nodes.items()})
        for entity, name in names.items():
            if entity not in web.registry.known_entities:
                continue
            key = fingerprint({"entity": entity, "field": "expense", "dates": [data.start_date, data.end_date]})[:24]
            web.gaps[key] = {"entity_id": entity, "field": "expense",
                "query": f"{name} accommodation meal transport prices {data.start_date} {data.end_date}",
                "resolved": False}
            if data.travellers.get("accessibility_needs"):
                key = fingerprint({"entity": entity, "field": "accessibility", "dates": [data.start_date, data.end_date]})[:24]
                web.gaps[key] = {"entity_id": entity, "field": "accessibility",
                    "query": f"{name} official accessibility information {data.start_date} {data.end_date}",
                    "resolved": False}
    return PlannerUtilities(routes=routes, web=web, entity_ids=entities, cost_facts=costs,
                            step_facts=steps, directed_minutes={}, fx_facts=saved_rates(data.evidence))
