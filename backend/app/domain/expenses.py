"""Deterministic currency conversion and expense aggregation."""

from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from app.models.draft import DraftCost
from app.models.evidence import CurrencyRateEvidence, FactStatus
from app.models.plan import MoneyRange

MONEY_QUANTUM = Decimal("0.01")


def convert_cost(
    cost: DraftCost,
    target_currency: str,
    rates: list[CurrencyRateEvidence],
) -> MoneyRange:
    """Convert an estimated local cost while preserving estimate semantics."""

    minimum = Decimal(str(cost.minimum))
    maximum = Decimal(str(cost.maximum))
    base = cost.currency_code.upper()
    target = target_currency.upper()
    if base == target:
        rate = Decimal("1")
        source_ids = list(cost.source_ids)
    else:
        match = next(
            (
                item
                for item in rates
                if item.base_currency == base and item.quote_currency == target
            ),
            None,
        )
        if match is None:
            return MoneyRange(
                minimum=Decimal("0"),
                maximum=Decimal("0"),
                currency_code=target,
                status=FactStatus.UNAVAILABLE,
                basis=(
                    f"Currency conversion unavailable; excluded original estimate "
                    f"{_plain_decimal(minimum)}-{_plain_decimal(maximum)} "
                    f"{base}. {cost.basis}"
                ),
                source_ids=[],
            )
        rate = match.rate
        source_ids = list(dict.fromkeys([*cost.source_ids, *match.source_ids]))

    return MoneyRange(
        minimum=(minimum * rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP),
        maximum=(maximum * rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP),
        currency_code=target,
        status=FactStatus.ESTIMATED,
        basis=cost.basis,
        source_ids=source_ids,
    )


def sum_money(
    costs: list[MoneyRange],
    currency_code: str,
    basis: str,
) -> MoneyRange:
    return MoneyRange(
        minimum=sum((cost.minimum for cost in costs), Decimal("0")),
        maximum=sum((cost.maximum for cost in costs), Decimal("0")),
        currency_code=currency_code,
        status=(
            FactStatus.UNAVAILABLE
            if any(cost.status == FactStatus.UNAVAILABLE for cost in costs)
            else FactStatus.ESTIMATED
        ),
        basis=basis,
        source_ids=sorted(
            {source_id for cost in costs for source_id in cost.source_ids}
        ),
    )


def category_totals(
    categorized_costs: list[tuple[str, MoneyRange]],
    currency_code: str,
) -> dict[str, MoneyRange]:
    grouped: defaultdict[str, list[MoneyRange]] = defaultdict(list)
    for category, cost in categorized_costs:
        grouped[category].append(cost)
    return {
        category: sum_money(costs, currency_code, f"Aggregated {category} estimates.")
        for category, costs in grouped.items()
    }


def _plain_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")
