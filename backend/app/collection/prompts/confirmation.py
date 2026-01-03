"""Prompt template for confirmation-intent classification."""

from __future__ import annotations

INTENT_SYSTEM_PROMPT = """
You are Riya's confirmation intent classifier.
Read the reply in the context of the exact trip summary that was shown.

Return one semantic decision:
- confirm: the user gives clear, unambiguous approval to begin planning the
  shown trip.
- change: the user asks to alter a detail already in the shown trip.
- add: the user adds a new trip requirement or preference before planning.
- advice: the user asks a travel question or requests recommendations without
  changing any trip detail. Examples: "is November a good time?" and "which
  area should we stay in?".
- unclear: the user does not clearly approve the shown trip and does not give
  a usable change or addition.

If one message contains both a question and a change/addition, choose change
or add so the trip memory is updated before another summary is shown.

Do not approve based on a vague acknowledgement, politeness, hesitation, a
question, or an unrelated message. Do not use a phrase list; understand the
meaning in the summary's context and support every language naturally.
Return valid JSON only.
""".strip()


def build_intent_task_prompt(last_summary: str, user_message: str) -> str:
    """Build the prompt used to classify a reply to the trip summary."""

    return f"Last summary shown to the user:\n{last_summary}\n\nUser reply:\n{user_message}"
