"""Pure calculations; these functions make no network calls."""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from .contracts import RouteLeg, ToolResult, ToolStatus
from .http import result


class CostItem(BaseModel):
    category: str
    amount: float | None = Field(default=None, ge=0)
    currency: str


class CostSummary(BaseModel):
    currency: str
    known_total: float
    category_totals: dict[str, float]
    unknown_categories: list[str]


def calculate_costs(items: list[CostItem], currency: str) -> ToolResult[CostSummary]:
    if any(item.currency.upper() != currency.upper() for item in items):
        raise ValueError("Convert all costs to one currency before totaling")
    totals: dict[str, float] = {}
    unknown: set[str] = set()
    for item in items:
        if item.amount is None:
            unknown.add(item.category)
        else:
            totals[item.category] = round(totals.get(item.category, 0) + item.amount, 2)
    summary = CostSummary(currency=currency.upper(), known_total=round(sum(totals.values()), 2),
                          category_totals=totals, unknown_categories=sorted(unknown))
    return result(summary, "calculation", warnings=["Some costs are unknown."] if unknown else [])


class Visit(BaseModel):
    place_id: str
    duration_minutes: int = Field(gt=0)
    opens_at: datetime | None = None
    closes_at: datetime | None = None


class ScheduledVisit(BaseModel):
    place_id: str
    start: datetime | None = None
    end: datetime | None = None
    status: ToolStatus
    warning: str | None = None


def schedule_visits(visits: list[Visit], routes: list[RouteLeg], start: datetime,
                    end: datetime, start_location_id: str) -> ToolResult[list[ScheduledVisit]]:
    if end <= start:
        raise ValueError("day end must follow day start")
    route_by_pair = {(r.origin_id, r.destination_id): r for r in routes}
    now, previous = start, start_location_id
    timing_unknown = False
    scheduled = []
    for visit in visits:
        leg = route_by_pair.get((previous, visit.place_id))
        if timing_unknown or leg is None or leg.duration_s is None:
            scheduled.append(ScheduledVisit(place_id=visit.place_id, status=ToolStatus.UNAVAILABLE,
                                            warning="Travel time is unknown."))
            timing_unknown = True
            previous = visit.place_id
            continue
        now += timedelta(seconds=leg.duration_s)
        if visit.opens_at and now < visit.opens_at:
            now = visit.opens_at
        finish = now + timedelta(minutes=visit.duration_minutes)
        if (visit.closes_at and finish > visit.closes_at) or finish > end:
            scheduled.append(ScheduledVisit(place_id=visit.place_id, status=ToolStatus.UNAVAILABLE,
                                            warning="Visit does not fit the available time."))
        else:
            scheduled.append(ScheduledVisit(place_id=visit.place_id, start=now, end=finish,
                                            status=ToolStatus.OK))
            now = finish
        previous = visit.place_id
    warnings = ["Some visits could not be scheduled."] if any(v.status != ToolStatus.OK for v in scheduled) else []
    return result(scheduled, "calculation", warnings=warnings)
