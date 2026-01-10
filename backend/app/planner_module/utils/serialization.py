"""Convert result data to console/API JSON without writing files."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any
from pydantic import BaseModel


def to_json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return to_json_value(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): to_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_json_value(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # Do not stringify clients/config objects: their repr may contain credentials.
    return {"type": type(value).__name__}
