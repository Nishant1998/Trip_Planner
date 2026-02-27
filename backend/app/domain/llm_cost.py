"""Request-local OpenAI token-cost estimates."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# USD per million tokens. Update this versioned table when model pricing changes;
# unknown models deliberately report zero instead of inventing a price.
OPENAI_MODEL_RATES: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-5.6-sol": (Decimal("5.00"), Decimal("30.00")),
    "gpt-5.6": (Decimal("5.00"), Decimal("30.00")),
    "gpt-5.6-terra": (Decimal("2.00"), Decimal("12.00")),
    "gpt-5.6-luna": (Decimal("0.20"), Decimal("1.20")),
}


def estimate_openai_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> Decimal:
    rates = OPENAI_MODEL_RATES.get(model)
    if rates is None:
        return Decimal("0")
    million = Decimal("1000000")
    value = (
        Decimal(input_tokens) * rates[0] / million
        + Decimal(output_tokens) * rates[1] / million
    )
    return value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
