"""Credential-free HTTP instrumentation for research and routing."""

from time import monotonic
import logging
from app.services.llm_usage import (
    ensure_current_trip_can_start_model_call,
    record_current_tool_usage,
)

logger = logging.getLogger(__name__)


async def request_started(request):
    ensure_current_trip_can_start_model_call()
    request.extensions["usage_started"] = monotonic()
    record_current_tool_usage("research_planning", request.url.host, "http_request")


async def response_received(response):
    elapsed = round(
        (monotonic() - response.request.extensions.get("usage_started", monotonic()))
        * 1000
    )
    record_current_tool_usage(
        "research_planning",
        response.request.url.host,
        "http_request",
        calls=0,
        failed_calls=int(response.is_error),
        duration_ms=elapsed,
    )
    logger.log(
        logging.WARNING if response.is_error else logging.DEBUG,
        "provider_response",
        extra={
            "provider": response.request.url.host,
            "status": response.status_code,
            "duration_ms": elapsed,
        },
    )


HTTP_HOOKS = {"request": [request_started], "response": [response_received]}
