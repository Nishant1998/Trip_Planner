"""Structured, secret-safe application logging for console and rotating files."""

import json
import logging
import os
import sys
import traceback
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.services.failure import sanitize_message

BACKEND_DIR = Path(__file__).resolve().parents[2]
_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
request_id_context: ContextVar[str] = ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    """Emit compact JSON records without request bodies or provider payloads."""

    def format(self, record):
        data = {
            "time": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "run_id": getattr(record, "run_id", request_id_context.get()),
            "event": sanitize_message(record.getMessage()),
        }
        for key in (
            "trip_id",
            "job_id",
            "duration_ms",
            "provider",
            "status",
            "attempt",
            "count",
            "error_type",
            "request_id",
            "origin_id",
            "destination_id",
            "provider_mode",
            "departure_time",
        ):
            if hasattr(record, key):
                data[key] = getattr(record, key)
        if record.exc_info:
            data["error_type"] = record.exc_info[0].__name__
            data["error"] = sanitize_message(str(record.exc_info[1]))
            data["stack"] = [
                {
                    "file": frame.filename,
                    "line": frame.lineno,
                    "function": frame.name,
                }
                for frame in traceback.extract_tb(record.exc_info[2])
            ]
        return json.dumps(data, default=str)


def _enabled(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _file_handler(formatter: logging.Formatter) -> logging.Handler | None:
    if not _enabled(os.getenv("LOG_FILE_ENABLED"), default=True):
        return None
    configured_directory = Path(os.getenv("LOG_DIRECTORY", "runtime/logs"))
    directory = (
        configured_directory
        if configured_directory.is_absolute()
        else BACKEND_DIR / configured_directory
    ).resolve()
    if not directory.is_relative_to(BACKEND_DIR):
        raise ValueError("LOG_DIRECTORY must stay inside the backend directory")
    directory.mkdir(parents=True, exist_ok=True)
    filename = os.getenv("LOG_FILE_NAME", "trip-planner.jsonl").strip()
    if not filename or Path(filename).name != filename:
        raise ValueError("LOG_FILE_NAME must be a simple filename")
    handler = RotatingFileHandler(
        directory / filename,
        maxBytes=_positive_int("LOG_FILE_MAX_BYTES", 10 * 1024 * 1024),
        backupCount=_positive_int("LOG_FILE_BACKUP_COUNT", 10),
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    return handler


def configure_logging() -> None:
    """Configure console plus optional rotating file logs from environment."""

    level = os.getenv("LOG_LEVEL", "INFO").upper()
    if level not in _VALID_LEVELS:
        raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
    formatter = JsonFormatter()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    handlers = [console]
    file_handler = _file_handler(formatter)
    if file_handler is not None:
        handlers.append(file_handler)
    logging.basicConfig(level=level, handlers=handlers, force=True)
    for name in ("httpx", "httpcore", "openai"):
        logging.getLogger(name).setLevel(logging.WARNING)
