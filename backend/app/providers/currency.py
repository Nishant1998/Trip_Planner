"""Frankfurter v2 exchange-rate adapter."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from app.models.evidence import (
    CurrencyRateEvidence,
    FactStatus,
    SourceRef,
)
from app.providers.http import ProviderHttpClient


class FrankfurterProvider:
    def __init__(self, timeout_seconds: float = 20) -> None:
        self._http = ProviderHttpClient(timeout_seconds)

    async def get_rate(
        self,
        base_currency: str,
        quote_currency: str,
    ) -> CurrencyRateEvidence:
        base = base_currency.upper()
        quote = quote_currency.upper()
        if base == quote:
            return CurrencyRateEvidence(
                id=f"currency-{base.lower()}-{quote.lower()}",
                base_currency=base,
                quote_currency=quote,
                rate=Decimal("1"),
                rate_date=date.today(),
                status=FactStatus.VERIFIED,
                source_ids=[],
            )
        response = await self._http.get(
            f"https://api.frankfurter.dev/v2/rate/{base}/{quote}"
        )
        payload = response.json()
        return CurrencyRateEvidence(
            id=f"frankfurter-{base.lower()}-{quote.lower()}",
            base_currency=base,
            quote_currency=quote,
            rate=Decimal(str(payload["rate"])),
            rate_date=date.fromisoformat(payload["date"]),
            status=FactStatus.VERIFIED,
            source_ids=["src-frankfurter-rates"],
        )

    @staticmethod
    def sources() -> list[SourceRef]:
        return [
            SourceRef(
                id="src-frankfurter-rates",
                provider="frankfurter",
                kind="currency",
                title="Frankfurter exchange-rate data",
                url="https://frankfurter.dev/",
                retrieved_at=datetime.now(UTC),
            )
        ]

    async def close(self) -> None:
        await self._http.close()
