"""Short-lived display content; durable plans remain the recovery authority."""

from collections import OrderedDict
from copy import deepcopy
import json
from time import monotonic

from .runtime import json_value


class EphemeralPlans:
    def __init__(self, *, ttl_seconds=600, max_entries=16, clock=monotonic):
        self.ttl_seconds, self.max_entries, self.clock = ttl_seconds, max_entries, clock
        self._plans = OrderedDict()

    def _expire(self):
        now = self.clock()
        for key, (expires, _) in list(self._plans.items()):
            if expires <= now:
                del self._plans[key]

    def put(self, plan):
        self._expire()
        payload = json_value(plan)
        if len(json.dumps(payload).encode("utf-8")) > 512_000:
            return  # Bound memory even if a provider returns unusually large metadata.
        self._plans[payload["plan_id"]] = (self.clock() + self.ttl_seconds, deepcopy(payload))
        while len(self._plans) > self.max_entries:
            self._plans.popitem(last=False)

    def get(self, plan_id):
        self._expire()
        entry = self._plans.get(plan_id)
        return deepcopy(entry[1]) if entry else None

    def clear(self):
        self._plans.clear()
