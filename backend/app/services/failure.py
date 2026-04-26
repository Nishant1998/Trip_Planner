"""Map one internal failure to safe public and diagnostic representations."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError

from app.models.failure import (
    FailureCode,
    FailureDiagnostic,
    PublicFailure,
)
from app.models.progress import PlanningStage
from app.exceptions import PlannerError


@dataclass(frozen=True, slots=True)
class MappedFailure:
    public: PublicFailure
    diagnostic: FailureDiagnostic


class FailureMapper:
    """Classify failures and remove secrets from developer diagnostics."""

    def __init__(self, secrets: Iterable[str | None] = ()) -> None:
        self.secrets = tuple(
            sorted(
                {secret for secret in secrets if secret and len(secret) >= 6},
                key=len,
                reverse=True,
            )
        )

    def map(
        self,
        error: BaseException,
        *,
        run_id: str,
        stage: PlanningStage | None,
        code: FailureCode | None = None,
        component: str | None = None,
        operation: str | None = None,
        attempt: int = 1,
    ) -> MappedFailure:
        root = _root_cause(error)
        resolved_code = code or _classify(error, root)
        return MappedFailure(
            public=PublicFailure(run_id=run_id, code=resolved_code),
            diagnostic=FailureDiagnostic(
                run_id=run_id,
                error_id=f"err_{uuid4().hex[:12]}",
                code=resolved_code,
                failed_stage=stage,
                component=component,
                operation=operation,
                attempt=attempt,
                exception_type=type(root).__name__,
                sanitized_message=sanitize_message(str(root), self.secrets),
                occurred_at=datetime.now(UTC),
            ),
        )


def sanitize_message(message: str, secrets: Iterable[str] = ()) -> str:
    """Return a compact diagnostic without common credential formats."""
    sanitized = " ".join(message.split())
    for secret in sorted({value for value in secrets if value}, key=len, reverse=True):
        sanitized = sanitized.replace(secret, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub(_redacted_match, sanitized)
    return sanitized[:2000] or "No diagnostic message was available."


def _classify(error: BaseException, root: BaseException) -> FailureCode:
    if isinstance(error, PlannerError):
        return error.failure_code
    if isinstance(root, ValidationError):
        return FailureCode.MODEL_OUTPUT_INVALID
    if isinstance(root, TimeoutError):
        return FailureCode.PROVIDER_UNAVAILABLE
    if isinstance(root, LookupError):
        return FailureCode.EVIDENCE_INSUFFICIENT
    # Recognise upstream provider errors (rate limits, quota, connection
    # errors) so a traveller sees a truthful reason instead of "internal".
    name = type(root).__name__
    if name in {
        "RateLimitError",
        "APIStatusError",
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ReadTimeout",
        "WriteTimeout",
    }:
        return FailureCode.PROVIDER_UNAVAILABLE
    return FailureCode.INTERNAL_ERROR


def _root_cause(error: BaseException) -> BaseException:
    current = error
    seen: set[int] = set()
    while current.__cause__ is not None and id(current) not in seen:
        seen.add(id(current))
        current = current.__cause__
    return current


def _redacted_match(match: re.Match[str]) -> str:
    prefix = match.groupdict().get("prefix") or ""
    return f"{prefix}[REDACTED]"


_SECRET_PATTERNS = (
    re.compile(r"(?P<prefix>\bBearer\s+)[^\s,;]+", re.IGNORECASE),
    re.compile(r"\b(?:sk-|gsk_)[A-Za-z0-9_-]{10,}"),
    re.compile(r"\blsv2_[A-Za-z0-9_-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9._=-]{20,}"),
    re.compile(
        r"(?P<prefix>\b(?:api[_-]?key|token|secret)\s*[:=]\s*)[^\s,;]+",
        re.IGNORECASE,
    ),
)
