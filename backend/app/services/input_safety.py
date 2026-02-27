"""Small deterministic privacy boundary before message storage or AI use."""

from __future__ import annotations

import re

_PRIVATE_PATTERNS = (
    re.compile(r"\b(?:sk|rk|pk)_[A-Za-z0-9_-]{16,}\b"),
    re.compile(
        r"\b(?:api[_ -]?key|password|secret|access[_ -]?token)\s*[:=]\s*\S+", re.I
    ),
    re.compile(r"\b(?:otp|one[ -]?time[ -]?password)\s*[:=]?\s*\d{4,8}\b", re.I),
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),
)


def contains_sensitive_data(text: str) -> bool:
    """Detect high-risk secrets; never send, persist, or log a matching message."""

    return any(pattern.search(text) is not None for pattern in _PRIVATE_PATTERNS)
