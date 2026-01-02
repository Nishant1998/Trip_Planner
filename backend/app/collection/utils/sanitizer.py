# Provides the shared user-facing response sanitizer that strips jargon, banned phrases, and URLs from any agent reply.
"""Shared user-facing response sanitizer.

Extracted from trip_orchestrator so it can be applied to *any*
ConversationAgentOutput — including those produced by direct agent
calls in live tests — without going through the orchestrator.
"""

from __future__ import annotations

import json
import re

# ── Constants ────────────────────────────────────────────────────────────────

_MAX_SENTENCES = 4

# Final user-facing terminology guard for model-generated replies.
# Rules are applied in order — put specific patterns before generic ones.
_USER_FACING_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    # ── OPENER BANS ───────────────────────────────────────────────────────────
    (r"^\s*(?:Certainly|Absolutely|Of course)[!,.]?\s*", ""),
    (r"^\s*Nice plan\b", "That sounds good"),
    (r"^\s*Nice\b", "Sounds good"),
    # ── ROBOTIC ASSISTANT PHRASES (remove full sentence containing them) ──────
    # Narrow "I have noted" rule — removes ONLY short, generic affirmations like
    # "I have noted your preference." or "I've recorded your input." (1–2 plain
    # words after "your").  The negative lookahead skips sentences that contain
    # a snake_case field name (those are handled by the field-name rules below)
    # and the {1,2}-word limit skips genuine informational sentences like
    # "I have noted your trip from Mumbai to Goa starting December 20th."
    (
        r"\bI(?:'ve| have) (?:noted|recorded) your (?!.*[a-z]+_[a-z_]+)(?:\w+\s*){1,2}[.!?]\s*",
        "",
    ),
    (r"Please provide\b[^.!?]*[.!?]?\s*", ""),
    (r"Kindly share\b[^.!?]*[.!?]?\s*", ""),
    # ── "SHALL/SHOULD/CAN I SET" DERIVED DATE QUESTION ───────────────────────
    # Removes the entire question so the empty-response guard can fire a fallback.
    (r"\b(?:should|shall|can) I set\b[^?]*\?", ""),
    # ── INTERCITY / ROUTE JARGON ──────────────────────────────────────────────
    (
        r"\bintercity travel between cities and temple stops\b",
        "travel between temples and planned stops",
    ),
    (r"\binter[- ]?city legs?\b", "travel between places"),
    (r"\blegs\b", "stops"),
    # ── SPENDING / HOTEL TIER LABELS ──────────────────────────────────────────
    # "mid-range budget" and "mid_range budget" → removed (don't label the tier)
    (r"\bmid[_-]range budget\b", ""),
    # Raw enum "mid_range" → removed; must come AFTER the phrase above.
    (r"\bmid_range\b", ""),
    # ── FIELD NAME REPLACEMENTS (known snake_case identifiers) ────────────────
    (r"\blong_distance_modes\b", "travel preferences"),
    (r"\bself_driving\b", "driving yourself"),
    (r"\btrain_class\b", "train class preference"),
    (r"\bflight_cabin_class\b", "cabin class"),
    (r"\btrip_budget\b", "your budget"),
    (r"\bnum_adults\b", "travellers"),
    (r"\bnum_children\b", "children in your group"),
    (r"\bnum_seniors\b", "seniors in your group"),
    (r"\btravel_style\b", "travel pace"),
    (r"\bspending_tier\b", "budget range"),
    (r"\bhotel_tier\b", "hotel preference"),
    (r"\btrip_purpose\b", "trip purpose"),
    (r"\blocal_transport_modes\b", "local transport preference"),
    (r"\bdepart_time_window\b", "departure time preference"),
    (r"\barrival_time_window\b", "arrival time preference"),
    (r"\breturn_time_window\b", "return time preference"),
    (r"\broom_type_pref\b", "room type preference"),
    (r"\blodging_type\b", "accommodation type"),
    (r"\bday_start_time\b", "start time for the day"),
    (r"\bday_end_time\b", "end time for the day"),
    (r"\btrip_duration_days\b", "trip length in days"),
    (r"\bactivity_interests\b", "activities you enjoy"),
    (r"\bdietary_restrictions\b", "dietary preferences"),
    (r"\baccessibility_needs\b", "accessibility requirements"),
    # ── ENUM VALUE REPLACEMENTS ───────────────────────────────────────────────
    (r"\bearly_morning\b", "early morning"),
    (r"\bac_3e\b", "AC 3E economy"),
    (r"\bac_first\b", "first class AC"),
    (r"\bac_3\b", "AC 3 tier"),
    (r"\bac_2\b", "AC 2 tier"),
    (r"\bchair_car\b", "chair car"),
    (r"\bexec_chair\b", "executive chair car"),
    (r"\bsecond_sitting\b", "second sitting class"),
    (r"\bpublic_transport\b", "public transport"),
    (r"\bfamily_visit\b", "family visit"),
    (r"\bweekend_trip\b", "weekend trip"),
    (r"\broad_trip_purpose\b", "road trip"),
    # ── URL AND LINK STRIPPING ────────────────────────────────────────────────
    # Bare https:// URLs — stop at whitespace or closing delimiters, NOT at dot.
    (r"\s+https?://[^\s)\"'<>]+", ""),
    # Outer-paren markdown hyperlinks: ([Wikipedia](https://...))
    (r"\s*\(\[[^\]]+\]\(https?://[^\)]+\)\)", ""),
    # Standalone markdown hyperlinks: [Wikipedia](https://...)
    (r"\s*\[[^\]]+\]\(https?://[^\)]+\)", ""),
    # Bare URL parentheticals: (https://example.com)
    (r"\s*\(https?://[^\)]+\)", ""),
    # Domain-only citation parens: (en.wikipedia.org)
    (r"\s*\([^)\s]+\.[a-z]{2,6}(?:/[^)]*)?(?:\s*\))", ""),
    # ── CATCH-ALL: any remaining snake_case identifiers ───────────────────────
    # Runs AFTER all specific replacements so known terms are already handled.
    # Matches patterns like some_field_name but not hyphenated words.
    (r"\b([a-z]+_[a-z][a-z_]*)\b", ""),
    # ── DOUBLE QUESTION GUARD: keep at most one "?" per response ─────────────
    # Removes a second question sentence that immediately follows the first "?".
    # e.g. "Where are you from? What dates?" → "Where are you from?"
    (r"\?(\s+[A-Z][^?]*\?)", "?"),
)


# ── Public helpers ────────────────────────────────────────────────────────────


def limit_sentences(text: str, max_sentences: int = _MAX_SENTENCES) -> str:
    """Trim a response to at most *max_sentences* complete sentences."""
    if not text:
        return text
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    if len(parts) <= max_sentences:
        return text.strip()
    return " ".join(parts[:max_sentences])


def _strip_raw_json_payloads(text: str) -> str:
    """Remove complete JSON objects or arrays accidentally returned as chat text.

    A JSON decoder is used instead of a non-greedy regular expression so nested
    payloads are removed as one unit and normal brackets in natural language
    remain untouched.
    """

    decoder = json.JSONDecoder()
    parts: list[str] = []
    position = 0
    while position < len(text):
        next_object = text.find("{", position)
        next_array = text.find("[", position)
        starts = [index for index in (next_object, next_array) if index >= 0]
        if not starts:
            parts.append(text[position:])
            break

        start = min(starts)
        parts.append(text[position:start])
        try:
            _, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            parts.append(text[start])
            position = start + 1
        else:
            position = start + end
    return "".join(parts)


def sanitize_user_facing_response(text: str) -> str:
    """Strip jargon, banned phrases, bare URLs, and enforce response hygiene.

    This is the canonical implementation shared by the orchestrator and any
    direct agent caller (e.g. live-test helpers).  Calling it twice on the
    same text is idempotent, so the orchestrator can safely keep its own call
    without double-mangling the text.
    """
    if not text:
        return text

    safe_text = _strip_raw_json_payloads(text)
    for pattern, replacement in _USER_FACING_REPLACEMENTS:
        safe_text = re.sub(pattern, replacement, safe_text, flags=re.IGNORECASE)

    safe_text = limit_sentences(safe_text.strip())

    if not safe_text.strip():
        return "Got it — what else would you like to share?"

    return safe_text.strip()
