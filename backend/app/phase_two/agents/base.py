"""Execution adapter, not an additional agent or graph node."""


class AgentRole:
    name: str

    def __init__(self, runtime):
        self.runtime = runtime

    async def __call__(self, state, task=None):
        method = {
            "supervisor": "supervisor",
            "destination_research": "research",
            "itinerary_planning": "itinerary",
            "accommodation": "accommodation",
            "transport_booking": "transport",
            "response_aggregator": "presentation",
        }[self.name]
        call = getattr(self.runtime, method)
        return (
            await call(state, task)
            if self.name in ("supervisor", "itinerary_planning")
            else await call(state)
        )
