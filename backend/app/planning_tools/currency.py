"""Frankfurter currency conversion; no API key required."""

from __future__ import annotations

import httpx

from .contracts import CurrencyInput, CurrencyQuote
from .http import HttpTool, failure, result


class CurrencyTool(HttpTool):
    async def convert(self, query: CurrencyInput):
        base, quote = query.base.upper(), query.quote.upper()
        if base == quote:
            return result(CurrencyQuote(base=base, quote=quote, rate=1, amount=query.amount,
                                        converted_amount=query.amount), "identity")
        try:
            response = await self.client.get(
                "https://api.frankfurter.dev/v1/latest", params={"base": base, "symbols": quote}
            )
            response.raise_for_status()
            payload = response.json()
            rate = float(payload["rates"][quote])
            return result(CurrencyQuote(
                base=base, quote=quote, rate=rate, amount=query.amount,
                converted_amount=round(query.amount * rate, 2), date=payload.get("date"),
            ), "frankfurter")
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            return failure("frankfurter", exc)
