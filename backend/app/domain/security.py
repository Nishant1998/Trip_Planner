"""Security utilities for user input validation and sensitive data detection."""

import re

_SENSITIVE_PATTERNS = [
    re.compile(r"\b(?:\d[ -]*?){13,16}\b"),  # Credit card numbers
    re.compile(
        r"\b(?:api[_-]?key|secret|password|passwd|token|bearer)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:sk-[a-zA-Z0-9]{32,}|AIza[0-9A-Za-z-_]{35})\b"
    ),  # OpenAI/Google API keys
    re.compile(
        r"(?:\b\d{6}\b.*\b(?:otp|verification|one[ -]?time[ -]?password)\b|"
        r"\b(?:otp|verification|one[ -]?time[ -]?password)\b\s*[:#-]?\s*\d{6}\b)",
        re.IGNORECASE,
    ),
]


def contains_sensitive_data(text: str) -> bool:
    """Check if the text contains sensitive credentials, credit cards, or passwords."""
    for pattern in _SENSITIVE_PATTERNS:
        if pattern.search(text):
            return True
    return False
