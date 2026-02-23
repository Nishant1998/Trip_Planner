"""Open-Meteo forecast adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Any

from app.models.evidence import (
    FactStatus,
    GeoPoint,
    SourceRef,
    WeatherEvidence,
)
from app.models.context import ResolvedStop
from app.providers.http import ProviderHttpClient


class OpenMeteoProvider:
    def __init__(
        self,
        geocode: Callable[[str], Awaitable[GeoPoint]],
        timeout_seconds: float = 20,
    ) -> None:
        self._geocode = geocode
        self._http = ProviderHttpClient(timeout_seconds)

    async def get_weather(
        self,
        stop: ResolvedStop,
        start_date: date,
        end_date: date,
    ) -> list[WeatherEvidence]:
        point = await self._geocode(stop.name)
        response = await self._http.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": point.latitude,
                "longitude": point.longitude,
                "daily": (
                    "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max"
                ),
                "timezone": "auto",
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
        )
        payload = response.json()
        daily = payload.get("daily") or {}
        dates = daily.get("time") or []
        result: list[WeatherEvidence] = []
        for index, raw_date in enumerate(dates):
            weather_date = date.fromisoformat(raw_date)
            result.append(
                WeatherEvidence(
                    id=f"open-meteo-{_slug(stop.name)}-{weather_date.isoformat()}",
                    stop_name=stop.name,
                    date=weather_date,
                    summary=_weather_label(_at(daily.get("weather_code"), index)),
                    minimum_temperature_c=_at(daily.get("temperature_2m_min"), index),
                    maximum_temperature_c=_at(daily.get("temperature_2m_max"), index),
                    precipitation_probability=_at(
                        daily.get("precipitation_probability_max"), index
                    ),
                    timezone=payload.get("timezone"),
                    status=FactStatus.VERIFIED,
                    source_ids=["src-open-meteo-forecast"],
                )
            )
        return result

    @staticmethod
    def sources() -> list[SourceRef]:
        return [
            SourceRef(
                id="src-open-meteo-forecast",
                provider="open-meteo",
                kind="weather",
                title="Open-Meteo forecast",
                url="https://open-meteo.com/en/docs",
                retrieved_at=datetime.now(UTC),
            )
        ]

    async def close(self) -> None:
        await self._http.close()


def _at(values: list[Any] | None, index: int) -> Any:
    return values[index] if values and index < len(values) else None


def _weather_label(code: int | None) -> str:
    if code is None:
        return "Forecast unavailable."
    if code == 0:
        return "Clear sky."
    if code in {1, 2, 3}:
        return "Partly cloudy."
    if code in {45, 48}:
        return "Foggy."
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "Rain is possible."
    if 71 <= code <= 77 or code in {85, 86}:
        return "Snow is possible."
    if code >= 95:
        return "Thunderstorms are possible."
    return "Mixed weather conditions."


def _slug(value: str) -> str:
    return "-".join(value.casefold().replace(",", " ").split())
