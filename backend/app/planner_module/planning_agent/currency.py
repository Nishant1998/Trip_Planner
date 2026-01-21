"""Use saved provider rates as disclosed estimates, never future-price guarantees."""
from decimal import Decimal, InvalidOperation


def saved_rates(evidence):
    pairs = {}
    for source in evidence:
        if source.get("kind") != "provider" or source.get("provider") != "frankfurter":
            continue
        currencies = str(source.get("subject", "")).split("/")
        if len(currencies) != 2 or any(len(value) != 3 or not value.isascii()
                                     or not value.isalpha() or not value.isupper() for value in currencies):
            continue
        value = source.get("retrieved_value")
        if not isinstance(value, dict) or not source.get("id"):
            continue
        try:
            rate = Decimal(str(value.get("rate")))
        except InvalidOperation:
            continue
        if not rate.is_finite() or not Decimal("1e-12") <= rate <= Decimal("1e12"):
            continue
        base, quote = currencies
        if base == quote:
            continue
        for left, right, amount in ((base, quote, rate), (quote, base, Decimal(1) / rate)):
            key = f"{left}/{right}"
            row = pairs.setdefault(key, {"rates": set(), "evidence_ids": set()})
            row["rates"].add(str(amount.normalize()))
            row["evidence_ids"].add(source["id"])
    return {key: {"rate": next(iter(row["rates"])) if len(row["rates"]) == 1 else None,
                  "evidence_ids": sorted(row["evidence_ids"]),
                  "status": "estimate" if len(row["rates"]) == 1 else "conflict"}
            for key, row in sorted(pairs.items())}


def convert_budget(request, rates):
    """Keep original items untouched and return an auditable conversion projection."""
    items, conversions, originals = [], [], {}
    for item in request.items:
        if item.id in originals and originals[item.id] != item:
            raise ValueError("Conflicting duplicate cost ID")
        originals[item.id] = item
        if item.currency == request.currency:
            items.append(item)
            continue
        fact = rates.get(f"{item.currency}/{request.currency}", {})
        rate = Decimal(fact["rate"]) if fact.get("rate") else None
        # Zero is currency independent, but unknown costs stay unknown.
        zero = item.lower == item.upper == 0 and item.basis != "unknown"
        known = rate is not None or zero
        converted = item.model_copy(update={"currency": request.currency,
            "lower": item.lower * (rate or 1) if known and item.lower is not None else None,
            "upper": item.upper * (rate or 1) if known and item.upper is not None else None,
            "basis": ("unknown" if not known else "estimate" if item.basis == "supplier_evidence" else item.basis)})
        items.append(converted)
        conversions.append({"item_id": item.id, "from_currency": item.currency,
            "to_currency": request.currency, "original_lower": str(item.lower) if item.lower is not None else None,
            "original_upper": str(item.upper) if item.upper is not None else None,
            "rate": str(rate) if rate is not None else None, "evidence_ids": fact.get("evidence_ids", []),
            "status": "zero_amount" if zero else fact.get("status", "unavailable"),
            "note": "Saved rate is an estimate; rate date, bank charges and future rate are not established."})
    return request.model_copy(update={"items": items}), conversions
