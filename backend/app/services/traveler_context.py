"""Build the small, planning-relevant portion of an account profile."""

from __future__ import annotations

from app.db.tables import UserRow


def build_traveler_context(user: UserRow | None) -> dict[str, str]:
    """Return current profile preferences that can safely guide a trip.

    Account and authentication data deliberately stay out of model context.
    Confirmed trip fields always take precedence over these background defaults.
    """

    if user is None:
        return {}

    values = {
        "home_city": user.home_city,
        "citizenship": user.citizenship,
        "spending_tier": user.spending_tier,
    }
    return {
        name: value.strip()
        for name, value in values.items()
        if isinstance(value, str) and value.strip()
    }
