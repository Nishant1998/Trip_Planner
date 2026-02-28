"""Create the immutable Phase 1 payload consumed by Phase 2."""

from __future__ import annotations

import hashlib
import json

from app.models.api import JsonObject
from app.models.memory import TripMemory
from app.models.state import serialize_runtime_memory


def build_confirmed_snapshot(memory: TripMemory) -> tuple[JsonObject, str]:
    """Return a JSON-safe canonical payload and content fingerprint.

    The payload is intentionally detached from live ``TripMemory``.  Later
    profile or metadata work may update current trip state, but it cannot
    mutate the already-approved Phase 2 input.
    """

    payload = json.loads(serialize_runtime_memory(memory))
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return payload, hashlib.sha256(canonical.encode("utf-8")).hexdigest()
