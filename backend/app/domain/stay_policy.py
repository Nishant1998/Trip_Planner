"""Truthful MVP stay-base wording without invented hotels or neighbourhoods."""

from __future__ import annotations


def approximate_stay_area(stop_name: str) -> str:
    """Return the deterministic area label used by routing and the UI."""
    return f"Central {stop_name} area (approximate base)"


def approximate_stay_description(stop_name: str) -> str:
    return (
        f"Use central {stop_name} as the temporary routing base. Choose and verify "
        "a suitable property, price, accessibility, and availability before booking; "
        "then recalculate daily routes from the confirmed stay."
    )
