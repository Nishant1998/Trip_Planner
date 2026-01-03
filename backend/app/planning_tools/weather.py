"""Open-Meteo forecast with bounded, clearly labelled historical estimates."""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, date, datetime, timedelta
from statistics import median
from typing import Callable

import httpx

from .contracts import (
    HistoricalWeatherSummary, ToolResult, ToolStatus, WeatherDay, WeatherInput,
    WeatherReport,
)
from .http import HttpTool, failure, unavailable


_FORECAST_DAILY = "weather_code,temperature_2m_min,temperature_2m_max,precipitation_probability_max"
_HISTORICAL_DAILY = "temperature_2m_min,temperature_2m_max,precipitation_sum"


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _at(daily: dict, key: str, index: int) -> object:
    values = daily.get(key)
    return values[index] if isinstance(values, list) and index < len(values) else None


def _mapped_day(day: date, year: int) -> date:
    """Map Feb 29 to Feb 28 in non-leap sample years."""
    try:
        return day.replace(year=year)
    except ValueError:
        return date(year, 2, 28)


def _sample_window(start: date, end: date, year: int) -> tuple[date, date]:
    sample_start = _mapped_day(start, year)
    end_year = year + (end.year - start.year)
    sample_end = _mapped_day(end, end_year)
    return sample_start, sample_end


class WeatherTool(HttpTool):
    def __init__(self, client: httpx.AsyncClient | None = None,
                 *, today_fn: Callable[[], date] = date.today) -> None:
        super().__init__(client)
        self.today_fn = today_fn

    async def forecast(self, query: WeatherInput) -> ToolResult[WeatherReport]:
        # Also protect callers that bypass Pydantic's normal input validation.
        if query.end_date < query.start_date or (query.end_date - query.start_date).days >= 31:
            return ToolResult(status=ToolStatus.ERROR, provider="weather",
                              retrieved_at=datetime.now(UTC), error_code="invalid_input",
                              warnings=["Weather dates must be ordered and span at most 31 days."])
        today = self.today_fn()
        if query.end_date < today:
            return unavailable("open_meteo", "Past trip dates are unavailable for planning estimates.",
                               "past_trip_dates")

        requested = [query.start_date + timedelta(days=i)
                     for i in range((query.end_date - query.start_date).days + 1)]
        future = [day for day in requested if day >= today]
        horizon = today + timedelta(days=15)
        forecast_days: list[WeatherDay] = []
        warnings: list[str] = []

        if future and future[0] <= horizon:
            try:
                response = await self.client.get(
                    "https://api.open-meteo.com/v1/forecast",
                    params={"latitude": query.location.latitude,
                            "longitude": query.location.longitude,
                            "daily": _FORECAST_DAILY, "timezone": "auto",
                            "start_date": future[0].isoformat(),
                            "end_date": min(future[-1], horizon).isoformat()},
                    timeout=20,
                )
                response.raise_for_status()
                forecast_days = self._forecast_days(response.json(), set(future))
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                warnings.append(f"Live forecast failed ({failure('open_meteo', exc).error_code}); "
                                "trying historical estimate.")

        forecast_dates = {day.date for day in forecast_days}
        missing_future = [day for day in future if day not in forecast_dates]
        historical_summary = None
        if missing_future:
            historical_summary = await self._historical_summary(
                query, missing_future[0], missing_future[-1], today)
            if historical_summary:
                warnings.append("Historical conditions are a broad estimate, not a forecast. "
                                "Wet-day frequency is not forecast rain probability.")
                expected_days = 0
                for year in historical_summary.sample_years:
                    sample_start, sample_end = _sample_window(
                        missing_future[0], missing_future[-1], year)
                    expected_days += (sample_end - sample_start).days + 1
                if historical_summary.sample_days < 0.8 * expected_days:
                    warnings.append(
                        f"Historical records cover only {historical_summary.sample_days} of "
                        f"{expected_days} expected sampled days; estimate is low confidence.")
            else:
                warnings.append("Historical estimate unavailable or based on fewer than five usable years.")

        past_dates = [day for day in requested if day < today]
        if past_dates:
            warnings.append("Past trip dates are unavailable for planning estimates.")
        if missing_future and historical_summary is None:
            warnings.append("No usable weather information covers some requested future dates.")

        if not forecast_days and historical_summary is None:
            return ToolResult(status=ToolStatus.UNAVAILABLE, provider="open_meteo",
                              retrieved_at=datetime.now(UTC), warnings=warnings,
                              error_code="weather_unavailable")

        report = WeatherReport(
            forecast_days=forecast_days,
            historical_summary=historical_summary,
            unavailable_dates=past_dates + (missing_future if historical_summary is None else []),
        )
        status = ToolStatus.OK if len(forecast_days) == len(requested) else ToolStatus.PARTIAL
        if status == ToolStatus.PARTIAL and missing_future:
            warnings.insert(0, "Live forecast does not cover every requested future day.")
        return ToolResult(status=status, data=report, provider="open_meteo",
                          retrieved_at=datetime.now(UTC), warnings=warnings)

    @staticmethod
    def _forecast_days(payload: dict, requested: set[date]) -> list[WeatherDay]:
        daily = payload.get("daily") or {}
        if not isinstance(daily, dict):
            raise ValueError("invalid forecast daily data")
        dates = daily.get("time") or []
        if not isinstance(dates, list):
            raise ValueError("invalid forecast dates")
        days: dict[date, WeatherDay] = {}
        for index, raw_date in enumerate(dates):
            day = date.fromisoformat(raw_date)
            low = _number(_at(daily, "temperature_2m_min", index))
            high = _number(_at(daily, "temperature_2m_max", index))
            if day not in requested or low is None or high is None or high < low:
                continue
            rain = _number(_at(daily, "precipitation_probability_max", index))
            code = _number(_at(daily, "weather_code", index))
            days[day] = WeatherDay(
                date=day, min_c=low, max_c=high,
                precipitation_probability=int(rain) if rain is not None and 0 <= rain <= 100 else None,
                weather_code=int(code) if code is not None else None,
            )
        return [days[day] for day in sorted(days)]

    async def _historical_summary(self, query: WeatherInput, start: date, end: date,
                                  today: date) -> HistoricalWeatherSummary | None:
        latest_year = today.year - 1
        # A Dec-Jan trip samples a Dec-Jan period wholly before the current year.
        if _sample_window(start, end, latest_year)[1] >= date(today.year, 1, 1):
            latest_year -= 1
        sample_years = list(range(latest_year - 9, latest_year + 1))
        semaphore = asyncio.Semaphore(3)

        async def fetch(year: int) -> tuple[int, list[tuple[float, float, float]]]:
            sample_start, sample_end = _sample_window(start, end, year)
            async with semaphore:
                try:
                    response = await self.client.get(
                        "https://archive-api.open-meteo.com/v1/archive",
                        params={"latitude": query.location.latitude,
                                "longitude": query.location.longitude,
                                "start_date": sample_start.isoformat(),
                                "end_date": sample_end.isoformat(),
                                "daily": _HISTORICAL_DAILY,
                                "models": "era5", "timezone": "auto"},
                        timeout=20,
                    )
                    response.raise_for_status()
                    return year, self._archive_days(response.json(), sample_start, sample_end)
                except (httpx.HTTPError, ValueError, TypeError, KeyError):
                    return year, []

        samples = await asyncio.gather(*(fetch(year) for year in sample_years))
        usable = [(year, days) for year, days in samples if days]
        if len(usable) < 5:
            return None
        all_days = [day for _, days in usable for day in days]
        return HistoricalWeatherSummary(
            start_date=start, end_date=end,
            typical_min_c=round(median(day[0] for day in all_days), 1),
            typical_max_c=round(median(day[1] for day in all_days), 1),
            wet_day_frequency_pct=round(100 * sum(day[2] >= 1 for day in all_days) / len(all_days), 1),
            sample_years=[year for year, _ in usable],
            sample_days=len(all_days),
        )

    @staticmethod
    def _archive_days(payload: dict, start: date, end: date) -> list[tuple[float, float, float]]:
        daily = payload.get("daily") or {}
        if not isinstance(daily, dict):
            raise ValueError("invalid archive daily data")
        dates = daily.get("time") or []
        if not isinstance(dates, list):
            raise ValueError("invalid archive dates")
        samples: dict[date, tuple[float, float, float]] = {}
        for index, raw_date in enumerate(dates):
            day = date.fromisoformat(raw_date)
            low = _number(_at(daily, "temperature_2m_min", index))
            high = _number(_at(daily, "temperature_2m_max", index))
            rain = _number(_at(daily, "precipitation_sum", index))
            if (start <= day <= end and low is not None and high is not None
                    and rain is not None and low <= high and rain >= 0):
                samples[day] = (low, high, rain)
        return list(samples.values())
