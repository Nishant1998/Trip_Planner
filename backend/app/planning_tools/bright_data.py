"""Bright Data scraper adapters for flight and hotel *search*, not booking.

Dataset IDs and input shapes come from the selected scraper's generated API
request. We never guess them or send a paid request during configuration.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from .contracts import (
    BookingOption,
    Coordinates,
    FlightSearchInput,
    HotelSearchInput,
    ToolResult,
    ToolStatus,
    TransportOption,
    TransportSegment,
)
from .http import HttpTool, failure, unavailable

_DATASET_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_TOKEN = re.compile(r"^\$([a-z_]+)$")
_FIELD_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+))*$")
_HOTEL_REQUIRED = {"name", "price", "currency", "price_basis", "url"}
_HOTEL_OPTIONAL = {"id", "availability", "address", "latitude", "longitude"}


def _render(value: Any, variables: dict[str, Any]) -> Any:
    """Replace only whole-value $variables; never evaluate user input as code."""
    if isinstance(value, str):
        match = _TOKEN.fullmatch(value)
        if match:
            key = match.group(1)
            if key not in variables:
                raise ValueError(f"Unknown scraper template variable: {key}")
            return variables[key]
        return value
    if isinstance(value, list):
        return [_render(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _render(item, variables) for key, item in value.items()}
    return value


def parse_template(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not parsed:
        raise ValueError("Scraper input template must be a non-empty JSON object")
    return parsed


def parse_hotel_output_fields(raw: str | None) -> dict[str, str] | None:
    """Bind hotel facts to documented source fields, never guessed aliases."""
    if not raw:
        return None
    fields = json.loads(raw)
    if (not isinstance(fields, dict) or not _HOTEL_REQUIRED <= fields.keys()
            or set(fields) - (_HOTEL_REQUIRED | _HOTEL_OPTIONAL)
            or any(not isinstance(path, str) or not _FIELD_PATH.fullmatch(path)
                   for path in fields.values())):
        raise ValueError("Hotel output fields must map required facts to source paths")
    return fields


def _records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("data", payload.get("records", payload.get("results")))
    if not isinstance(payload, list):
        raise ValueError("Scraper response does not contain a record list")
    return [record for record in payload if isinstance(record, dict)]


class BrightDataScraperClient(HttpTool):
    """One bounded collection per search; polling never triggers another job."""

    def __init__(self, token: str, client: httpx.AsyncClient | None = None, *,
                 max_records: int = 10, max_polls: int = 3, poll_delay_s: float = 2.0,
                 max_jobs: int = 8):
        super().__init__(client)
        self.token = token
        self.max_records = max_records
        self.max_polls = max_polls
        self.poll_delay_s = poll_delay_s
        self.max_jobs = max_jobs
        self.jobs_started = 0
        self.journal = None

    async def scrape(self, dataset_id: str, scraper_input: dict[str, Any], *,
                     discover_by: str | None = None, limit: int | None = None) -> ToolResult[list[dict[str, Any]]]:
        if self.journal is not None:
            key = hashlib.sha256(json.dumps([dataset_id, scraper_input, discover_by, limit],
                                            sort_keys=True, default=str).encode()).hexdigest()
            async def submit(received):
                return await self._scrape(dataset_id, scraper_input, discover_by=discover_by,
                                          limit=limit, on_snapshot=received)
            return await self.journal.collect(key, submit, self.resume)
        return await self._scrape(dataset_id, scraper_input, discover_by=discover_by, limit=limit)

    async def _scrape(self, dataset_id: str, scraper_input: dict[str, Any], *,
                      discover_by: str | None = None, limit: int | None = None, on_snapshot=None) -> ToolResult[list[dict[str, Any]]]:
        if not _DATASET_ID.fullmatch(dataset_id):
            return unavailable("bright_data", "Invalid scraper dataset ID.", "invalid_configuration")
        if discover_by not in (None, "search_input", "input_filters"):
            return unavailable("bright_data", "Invalid scraper discovery mode.", "invalid_configuration")
        if limit is not None and not 1 <= limit <= self.max_records:
            return unavailable("bright_data", "Invalid result limit.", "invalid_input")
        if self.jobs_started >= self.max_jobs:
            return unavailable("bright_data", "Per-run scraper job limit reached.", "run_limit")
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        params = {"dataset_id": dataset_id, "format": "json"}
        if discover_by:
            params.update({"type": "discover_new", "discover_by": discover_by,
                           "notify": "false", "include_errors": "true"})
        body = {"input": [scraper_input], "limit_per_input": limit or self.max_records}
        try:
            self.jobs_started += 1
            response = await self.client.post(
                "https://api.brightdata.com/datasets/v3/scrape", params=params,
                headers=headers, json=body, timeout=65,
            )
            response.raise_for_status()
            if response.status_code == 200:
                return self._finished(response.json())
            if response.status_code != 202:
                raise ValueError("Unexpected scraper response status")
            snapshot_id = response.json().get("snapshot_id")
            if not isinstance(snapshot_id, str) or not _DATASET_ID.fullmatch(snapshot_id):
                raise ValueError("Scraper did not return a valid snapshot ID")
            if on_snapshot is not None:
                await on_snapshot(snapshot_id)
            return await self.resume(snapshot_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (402, 429):
                code = "credit_exhausted" if exc.response.status_code == 402 else "rate_limited"
                return unavailable("bright_data", "Scraper provider is currently unavailable.", code)
            return failure("bright_data", exc)
        except Exception as exc:
            return failure("bright_data", exc)

    async def resume(self, snapshot_id: str) -> ToolResult[list[dict[str, Any]]]:
        """Continue an existing collection without starting or billing a new one."""
        if not isinstance(snapshot_id, str) or not _DATASET_ID.fullmatch(snapshot_id):
            return unavailable("bright_data", "Invalid snapshot ID.", "invalid_input")
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            for _ in range(self.max_polls):
                await asyncio.sleep(self.poll_delay_s)
                progress = await self.client.get(
                    f"https://api.brightdata.com/datasets/v3/progress/{snapshot_id}",
                    headers=headers, timeout=15,
                )
                progress.raise_for_status()
                state = progress.json().get("status")
                if state == "ready":
                    downloaded = await self.client.get(
                        f"https://api.brightdata.com/datasets/v3/snapshot/{snapshot_id}",
                        params={"format": "json"}, headers=headers, timeout=30,
                    )
                    downloaded.raise_for_status()
                    return self._finished(downloaded.json())
                if state == "failed":
                    return unavailable("bright_data", "Scraper job failed.", "job_failed")
                if state not in ("running", "pending", "starting", "collecting"):
                    return unavailable("bright_data", "Unknown scraper job state.", "unknown_job_state")
            return ToolResult(status=ToolStatus.PARTIAL, provider="bright_data",
                              retrieved_at=datetime.now(UTC),
                              warnings=["Scraper job is still running; no quote was returned."],
                              error_code="job_pending", continuation_token=snapshot_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (402, 429):
                code = "credit_exhausted" if exc.response.status_code == 402 else "rate_limited"
                return unavailable("bright_data", "Scraper provider is currently unavailable.", code)
            return failure("bright_data", exc)
        except Exception as exc:
            return failure("bright_data", exc)

    @staticmethod
    def _finished(payload: Any) -> ToolResult[list[dict[str, Any]]]:
        records = _records(payload)
        return ToolResult(status=ToolStatus.OK if records else ToolStatus.PARTIAL,
                          data=records, provider="bright_data", retrieved_at=datetime.now(UTC),
                          warnings=[] if records else ["Scraper returned no records."])


def _value(record: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        value: Any = record
        for part in path.split("."):
            if isinstance(value, list) and part.isdigit():
                index = int(part)
                value = value[index] if index < len(value) else None
            elif isinstance(value, dict):
                value = value.get(part)
            else:
                value = None
                break
        if value not in (None, ""):
            return value
    return None


def _price(value: Any) -> float | None:
    if isinstance(value, dict):
        value = _value(value, "amount", "value", "total", "price")
    if isinstance(value, str):
        cleaned = value.replace(",", "").strip()
        match = re.fullmatch(r"(?:[A-Z]{3}\s*)?[^0-9-]*([0-9]+(?:\.[0-9]+)?)\s*(?:[A-Z]{3})?", cleaned)
        if not match:
            return None
        value = match.group(1)
    try:
        number = float(value)
        return number if number >= 0 else None
    except (TypeError, ValueError):
        return None


def _datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    return value if parsed.scheme == "https" and parsed.netloc else None


def _id(record: dict[str, Any]) -> str:
    provided = _value(record, "itinerary_id", "listing_id", "hotel_id", "id")
    if provided is not None:
        return str(provided)
    fingerprint = json.dumps(record, sort_keys=True, default=str).encode()
    return hashlib.sha256(fingerprint).hexdigest()[:20]


def _booking_quote(record: dict[str, Any], query: HotelSearchInput) -> tuple[float | None, str | None, str]:
    """Read a dated, guest-matched offer from Booking's observed pricing structure."""
    if (str(record.get("check_in", ""))[:10] != query.check_in.isoformat()
            or str(record.get("check_out", ""))[:10] != query.check_out.isoformat()):
        return None, None, "unknown"
    url = _url(record.get("url"))
    url_params = parse_qs(urlparse(url).query) if url else {}
    source_rooms = url_params.get("no_rooms", [None])[0]
    if source_rooms is not None and source_rooms != str(query.rooms):
        return None, None, "unknown"
    if query.rooms > 1 and source_rooms is None:
        return None, None, "unknown"
    price_type = url_params.get("sb_price_type", [None])[0]
    basis = price_type if price_type in ("total", "per_night") else "unknown"
    nights = (query.check_out - query.check_in).days
    offers: list[tuple[float, str]] = []
    for room in record.get("pricing") or []:
        if not isinstance(room, dict):
            continue
        for offer in room.get("offers") or []:
            if not isinstance(offer, dict):
                continue
            occupancy = offer.get("occupancy") or {}
            quote = offer.get("price") or {}
            if (not isinstance(occupancy, dict) or not isinstance(quote, dict)
                    or occupancy.get("adults") != query.adults
                    or occupancy.get("children") != query.children
                    or quote.get("nights") != nights):
                continue
            price = _price(quote.get("final_price"))
            currency = quote.get("currency")
            # A live Booking record contained 1.141 USD beside 250 USD offers.
            # Its decimal/grouping meaning is ambiguous; do not quote it as $1.14.
            if price is not None and round(price, 2) != price:
                continue
            # The same live source also emitted 1.31 USD for a two-night Paris
            # hotel. Suppress ambiguous tiny totals in these currencies instead
            # of interpreting a possible thousands separator as cents.
            if price is not None and price < 10 and currency in ("USD", "EUR", "GBP"):
                continue
            if price is not None and isinstance(currency, str) and re.fullmatch(r"[A-Za-z]{3}", currency):
                offers.append((price, currency.upper()))
    if not offers:
        return None, None, "unknown"
    preferred = [offer for offer in offers if offer[1] == query.currency] if query.currency else []
    selected_pool = preferred or [offer for offer in offers if offer[1] == offers[0][1]]
    price, currency = min(selected_pool, key=lambda offer: offer[0])
    return price, currency, basis


class BrightDataFlightProvider:
    def __init__(self, scraper: BrightDataScraperClient, dataset_id: str,
                 input_template: dict[str, Any]):
        self.scraper = scraper
        self.dataset_id = dataset_id
        self.input_template = input_template

    async def search(self, query: FlightSearchInput) -> ToolResult[list[TransportOption]]:
        if query.continuation_token:
            raw = await self.scraper.resume(query.continuation_token)
        else:
            scraper_input = _render(self.input_template, {
                "origin": query.origin, "destination": query.destination,
                "departure_date": query.departure_date.isoformat(), "adults": query.adults,
                "departure_timestamp": query.departure_date.isoformat() + "T00:00:00.000Z",
                "trip_type": query.trip_type, "children": query.children,
                "infants_in_seat": query.infants_in_seat, "infants_on_lap": query.infants_on_lap,
                "cabin": query.cabin, "currency": query.currency,
                "language": query.language, "country": query.country,
            })
            if query.trip_type == "Round trip":
                scraper_input["return"] = query.return_date.isoformat() + "T00:00:00.000Z"
            else:
                scraper_input.pop("return", None)
            raw = await self.scraper.scrape(self.dataset_id, scraper_input,
                                            discover_by="input_filters", limit=query.result_limit)
        if raw.data is None:
            return ToolResult[list[TransportOption]](**raw.model_dump())
        options = []
        for item in raw.data:
            discovery = item.get("discovery_input") or {}
            if isinstance(discovery, dict) and any(
                key in discovery and discovery[key] != expected
                for key, expected in (("origin", query.origin), ("destination", query.destination),
                                      ("adults", query.adults), ("children", query.children),
                                      ("trip_type", query.trip_type))
            ):
                continue
            raw_legs = item.get("legs")
            if not isinstance(raw_legs, list) or not raw_legs or any(not isinstance(leg, dict) for leg in raw_legs):
                continue
            if (query.trip_type == "Round trip" and len(raw_legs) != 2) or (
                query.trip_type == "One way" and len(raw_legs) != 1
            ):
                continue
            first_date = _datetime(raw_legs[0].get("date"))
            if first_date is None or first_date.date() != query.departure_date:
                continue
            if query.trip_type == "Round trip":
                return_leg_date = _datetime(raw_legs[1].get("date"))
                if return_leg_date is None or return_leg_date.date() != query.return_date:
                    continue
            segments = []
            for leg in raw_legs:
                for segment in leg.get("segments") or []:
                    if not isinstance(segment, dict):
                        continue
                    segments.append(TransportSegment(
                        mode="flight", origin=segment.get("depart_airport_iata"),
                        destination=segment.get("arrive_airport_iata"),
                        departure_local=segment.get("depart_time"),
                        arrival_local=segment.get("arrive_time"),
                        carrier=segment.get("airline_name"),
                        vehicle=segment.get("flight_number"),
                    ))
            if not segments:
                continue
            price = _price(_value(item, "pricing.best_total_price"))
            currency = _value(item, "pricing.currency")
            fare_currency = str(currency).upper() if isinstance(currency, str) and re.fullmatch(r"[A-Za-z]{3}", currency) else None
            stop_count = 0
            for leg in raw_legs:
                try:
                    stop_count += int(leg.get("stops", 0))
                except (TypeError, ValueError):
                    stop_count = None
                    break
            options.append(TransportOption(
                id=_id(item), requested_mode="flight", origin=query.origin,
                destination=query.destination, segments=segments,
                departure_local=raw_legs[0].get("depart_local"),
                arrival_local=raw_legs[-1].get("arrive_local"),
                departure_date=first_date.date(), stops=stop_count,
                fare_amount=price if fare_currency else None,
                fare_currency=fare_currency if price is not None else None,
                fare_classification="quoted" if price is not None and fare_currency else "unknown",
                fare_scope="unknown",
                source_url=_url(_value(item, "url", "booking_url")),
                warnings=["Flight times are local to airports; timezone offsets were not supplied.",
                          "Scraped fare scope and availability are unverified; do not treat as a group total."],
            ))
        warnings = [*raw.warnings, "Scraped flight fares are not ticket availability or reservations."]
        if query.trip_type == "Round trip" and len(options) < len(raw.data):
            warnings.append("Outbound-only or unusable records were excluded from the round-trip result.")
        if any(option.fare_classification == "unknown" for option in options):
            warnings.append("Some flight fares or currencies were unavailable.")
        if not options:
            warnings.append("No usable flight itinerary was returned.")
        return ToolResult(status=ToolStatus.PARTIAL if options else ToolStatus.UNAVAILABLE,
                          data=options, provider="bright_data_google_flights",
                          retrieved_at=raw.retrieved_at, warnings=warnings)


class BrightDataHotelProvider:
    def __init__(self, scraper: BrightDataScraperClient, dataset_id: str,
                 input_template: dict[str, Any], output_fields: dict[str, str],
                 discover_by: str | None = None, booking_pricing: bool = False):
        self.scraper = scraper
        self.dataset_id = dataset_id
        self.input_template = input_template
        self.output_fields = parse_hotel_output_fields(json.dumps(output_fields))
        self.discover_by = discover_by
        self.booking_pricing = booking_pricing

    async def search(self, query: HotelSearchInput) -> ToolResult[list[BookingOption]]:
        if self.booking_pricing and query.check_in < date.today():
            return ToolResult(status=ToolStatus.ERROR, provider="bright_data_booking",
                              error_code="invalid_input",
                              warnings=["Check-in must be today or a future date."])
        if self.booking_pricing and query.children and self.input_template.get("children") != "$children":
            return unavailable("bright_data_booking",
                               "The configured Booking input does not include child guests; no search was started.",
                               "unsupported_guests")
        if self.booking_pricing and query.rooms != 1 and self.input_template.get("rooms") != "$rooms":
            return unavailable("bright_data_booking",
                               "The configured Booking input does not include the requested room count; no search was started.",
                               "unsupported_rooms")
        if self.booking_pricing and query.country and self.input_template.get("country") != "$country_or_blank":
            return unavailable("bright_data_booking",
                               "The configured Booking input does not include the requested country; no search was started.",
                               "unsupported_country")
        if query.continuation_token:
            raw = await self.scraper.resume(query.continuation_token)
        else:
            scraper_input = _render(self.input_template, {
                "destination": query.destination, "check_in": query.check_in.isoformat(),
                "check_out": query.check_out.isoformat(), "adults": query.adults,
                "children": query.children, "rooms": query.rooms,
                "currency": query.currency, "country_or_blank": query.country or "",
                "check_in_timestamp": query.check_in.isoformat() + "T00:00:00.000Z",
                "check_out_timestamp": query.check_out.isoformat() + "T00:00:00.000Z",
                "currency_or_blank": query.currency or "",
            })
            raw = await self.scraper.scrape(self.dataset_id, scraper_input,
                                            discover_by=self.discover_by)
        if raw.data is None:
            return ToolResult[list[BookingOption]](**raw.model_dump())
        options = []
        skipped_without_name = 0
        skipped_wrong_destination = 0
        skipped_wrong_guests = 0
        budget_unknown = False
        filtered_over_budget = 0
        nights = (query.check_out - query.check_in).days
        fields = self.output_fields
        for item in raw.data:
            if self.booking_pricing:
                discovery_input = item.get("discovery_input")
                source_destination = (discovery_input.get("location")
                                      if isinstance(discovery_input, dict) else None)
                if (isinstance(source_destination, str) and source_destination.strip()
                        and source_destination.strip().casefold() != query.destination.casefold()):
                    skipped_wrong_destination += 1
                    continue
                if isinstance(discovery_input, dict) and any(
                    field in discovery_input and discovery_input[field] != requested
                    for field, requested in (("adults", query.adults), ("children", query.children),
                                             ("rooms", query.rooms))
                ):
                    skipped_wrong_guests += 1
                    continue
            name = _value(item, fields["name"])
            if not isinstance(name, str) or not name.strip():
                skipped_without_name += 1
                continue
            if self.booking_pricing:
                price, currency, basis = _booking_quote(item, query)
                indicative_price, indicative_currency, indicative_basis = None, None, "unknown"
                if query.children and price is None:
                    adult_price, adult_currency, adult_basis = _booking_quote(
                        item, query.model_copy(update={"children": 0}))
                    if adult_price is not None and adult_basis != "unknown":
                        indicative_price, indicative_currency, indicative_basis = (
                            adult_price, adult_currency, adult_basis)
            else:
                price = _price(_value(item, fields["price"]))
                currency = _value(item, fields["currency"])
                currency = currency.upper() if isinstance(currency, str) and re.fullmatch(r"[A-Za-z]{3}", currency) else None
                basis = _value(item, fields["price_basis"])
                basis = basis if basis in ("total", "per_night") else "unknown"
            address = _value(item, fields["address"]) if "address" in fields else None
            latitude = _value(item, fields["latitude"]) if "latitude" in fields else None
            longitude = _value(item, fields["longitude"]) if "longitude" in fields else None
            try:
                location = Coordinates(latitude=latitude, longitude=longitude) if latitude is not None and longitude is not None else None
            except ValueError:
                location = None
            source_id = _value(item, fields["id"]) if "id" in fields else None
            option_id = str(source_id) if source_id is not None else hashlib.sha256(
                json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()[:20]
            option = BookingOption(
                id=option_id, name=name.strip(), price=price, price_basis=basis,
                currency=currency, location=location,
                indicative_price=indicative_price if self.booking_pricing else None,
                indicative_currency=indicative_currency if self.booking_pricing else None,
                indicative_price_basis=indicative_basis if self.booking_pricing else "unknown",
                indicative_price_scope="adult_only" if self.booking_pricing and indicative_price is not None else None,
                address=address if isinstance(address, str) else None,
                booking_url=_url(_value(item, fields["url"])),
                availability="available" if "availability" in fields and _value(item, fields["availability"]) == "available" else "unknown",
            )
            if query.budget_per_night is not None:
                comparable = (query.currency is not None and option.currency == query.currency
                              and option.price is not None and option.price_basis != "unknown")
                if comparable:
                    per_night = option.price / nights if option.price_basis == "total" else option.price
                    if per_night > query.budget_per_night:
                        filtered_over_budget += 1
                        continue
                else:
                    budget_unknown = True
            options.append(option)
        warnings = [*raw.warnings, "Scraped hotel prices and availability must be rechecked before booking."]
        if skipped_without_name:
            warnings.append(f"Skipped {skipped_without_name} record(s) without a hotel name.")
        if skipped_wrong_destination:
            warnings.append(f"Skipped {skipped_wrong_destination} record(s) from a different destination.")
        if skipped_wrong_guests:
            warnings.append(f"Skipped {skipped_wrong_guests} record(s) for different guests or rooms.")
        if filtered_over_budget:
            warnings.append(f"Excluded {filtered_over_budget} hotel(s) above the comparable per-night budget.")
        if budget_unknown:
            warnings.append("budget was not applied to some hotels because price, currency, or verified basis was unavailable.")
        if query.currency and any(
            currency and currency != query.currency
            for option in options for currency in (option.currency, option.indicative_currency)
        ):
            warnings.append("Provider returned some hotel quotes in a different currency than requested.")
        if any(option.price is None or option.currency is None or option.price_basis == "unknown" for option in options):
            warnings.append("Some hotel quotes lack a price, currency, or verified price basis.")
        if self.booking_pricing and query.children and options and all(option.price is None for option in options):
            warnings.append("No returned room offer matched the requested child guests; family prices are unknown.")
        if any(option.indicative_price is not None for option in options):
            warnings.append("Adult-only indicative prices exclude child charges; do not use them as family totals or for strict budget checks.")
        return ToolResult(status=ToolStatus.PARTIAL if warnings else raw.status,
                          data=options, provider="bright_data_booking",
                          retrieved_at=raw.retrieved_at, warnings=warnings)
