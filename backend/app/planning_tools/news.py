"""Optional events-search boundary; no provider has been selected."""

from __future__ import annotations

from datetime import date
from datetime import date as Date
from typing import Protocol

from pydantic import BaseModel

from .contracts import ToolResult
from .http import failure, unavailable


class EventsInput(BaseModel):
    destination: str
    start_date: date
    end_date: date


class Event(BaseModel):
    id: str
    name: str
    date: Date | None = None
    venue: str | None = None
    source_url: str | None = None


class EventsProvider(Protocol):
    async def search(self, query: EventsInput) -> ToolResult[list[Event]]: ...


class EventsSearchTool:
    def __init__(self, provider: EventsProvider | None = None):
        self.provider = provider

    async def search(self, query: EventsInput) -> ToolResult[list[Event]]:
        if self.provider is None:
            return unavailable("events_search", "No events or news API is configured.")
        try:
            return ToolResult[list[Event]].model_validate(await self.provider.search(query))
        except Exception as exc:
            return failure("events_search", exc)
