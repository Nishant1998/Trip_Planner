"""Small, deterministic country-currency lookup used before any FX API request.

This is country metadata, not trip-specific business logic.  Unknown countries stay
unknown: the agent must never guess an exchange rate or a currency.
"""

from __future__ import annotations

COUNTRY_CURRENCIES = {
    "india": "INR",
    "japan": "JPY",
    "united states": "USD",
    "usa": "USD",
    "united kingdom": "GBP",
    "uk": "GBP",
    "france": "EUR",
    "germany": "EUR",
    "italy": "EUR",
    "spain": "EUR",
    "australia": "AUD",
    "canada": "CAD",
    "singapore": "SGD",
    "thailand": "THB",
    "uae": "AED",
    "united arab emirates": "AED",
}


def destination_currency(
    destination_items: list[str], destination_type: str
) -> str | None:
    """Return a local currency only when destination metadata identifies a country."""
    if destination_type != "country" or not destination_items:
        return None
    return COUNTRY_CURRENCIES.get(destination_items[0].strip().lower())
