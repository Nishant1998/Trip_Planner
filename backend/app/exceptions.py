"""Application exception hierarchy."""

from __future__ import annotations

from typing import Self

from app.models.failure import FailureCode, FailureDiagnostic, PublicFailure


class CollectionError(RuntimeError):
    """Base runtime exception for recoverable collection-phase failures."""


class TripBusyError(CollectionError):
    def __init__(
        self, message: str = "A previous message is still processing for this trip"
    ) -> None:
        self.message = message
        super().__init__(message)


RiyaRuntimeError = CollectionError


class AgentRunError(CollectionError):
    def __init__(self, agent_name: str, cause: Exception) -> None:
        self.agent_name = agent_name
        self.cause = cause
        super().__init__(
            f"Agent '{agent_name}' failed: {type(cause).__name__}: {cause}"
        )


class RepositoryError(CollectionError):
    def __init__(self, operation: str, cause: Exception) -> None:
        self.operation = operation
        self.cause = cause
        super().__init__(
            f"Repository operation '{operation}' failed: {type(cause).__name__}: {cause}"
        )


class TurnTimeoutError(CollectionError):
    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"Collection turn exceeded the {timeout_seconds:.1f}s runtime budget"
        )


class PlannerError(Exception):
    code = "PLANNER_ERROR"
    status_code = 500
    failure_code = FailureCode.INTERNAL_ERROR

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.public_failure: PublicFailure | None = None
        self.diagnostic: FailureDiagnostic | None = None

    def attach_failure(
        self, public: PublicFailure, diagnostic: FailureDiagnostic
    ) -> Self:
        self.public_failure = public
        self.diagnostic = diagnostic
        return self


class PlannerLimitError(PlannerError):
    code = "PLANNER_LIMIT_EXCEEDED"
    status_code = 422
    failure_code = FailureCode.INVALID_INPUT


class PlannerTimeoutError(PlannerError):
    code = "PLANNER_TIMEOUT"
    status_code = 504
    failure_code = FailureCode.PLANNING_TIMEOUT


class PlannerExecutionError(PlannerError):
    code = "PLANNER_EXECUTION_FAILED"
    status_code = 500
    failure_code = FailureCode.INTERNAL_ERROR


class PlannerFinalReviewError(PlannerError):
    code = "PLANNER_FINAL_REVIEW_BLOCKED"
    status_code = 422
    failure_code = FailureCode.FINAL_REVIEW_BLOCKED
