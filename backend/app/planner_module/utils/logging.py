"""Console-only logs: stage summaries at INFO, intermediate outputs at DEBUG."""

from __future__ import annotations

import json
import logging
import sys
import traceback
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from time import perf_counter

from .serialization import to_json_value

run_id: ContextVar[str] = ContextVar("planner_run_id", default="-")
stage_name: ContextVar[str] = ContextVar("planner_stage", default="run")
logger = logging.getLogger("app.planner_module.workflow")

# Only graph data is printable. Credentials, settings and client objects are absent.
OUTPUT_KEYS = {
    "trip",
    "stops",
    "weather",
    "finance",
    "candidates",
    "shortlist",
    "pois",
    "clusters",
    "routes",
    "quality",
    "repair_pass",
    "report",
    "input",
    "world",
    "strategy",
    "result",
    "before_issues",
    "final_issues",
    "final_validation",
    "feasibility_actions",
    "reviewer_edits",
    "reviewer_decisions",
    "reviewer_findings",
    "route_trace",
    "route_plan",
    "mandatory_allocation",
    "candidate_scores",
    "utilization",
}


class ConsoleFormatter(logging.Formatter):
    def format(self, record):
        data = {
            "time": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "run_id": run_id.get(),
            "stage": stage_name.get(),
            "event": record.getMessage(),
        }
        for key in (
            "duration_ms",
            "provider",
            "attempt",
            "count",
            "status",
            "critical_gaps",
            "optional_gaps",
            "cache_hits",
            "cache_misses",
            "collection",
            "output",
            "fields",
            "tool",
            "agent_turn",
            "checkpoint_count",
            "submission_count",
            "repair_count",
        ):
            if hasattr(record, key):
                data[key] = to_json_value(getattr(record, key))
        if record.exc_info:
            data["error_type"] = record.exc_info[0].__name__
            data["stack"] = [
                {"file": frame.filename, "line": frame.lineno, "function": frame.name}
                for frame in traceback.extract_tb(record.exc_info[2])
            ]
        return json.dumps(data, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Call at the console entry point, or let your backend configure its own logs."""
    level = level.upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("Log level must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
    package_logger = logging.getLogger("app.planner_module")
    package_logger.setLevel(level)
    package_logger.propagate = False
    if not any(
        getattr(handler, "_planner_console", False)
        for handler in package_logger.handlers
    ):
        handler = logging.StreamHandler(sys.stdout)
        handler._planner_console = True
        handler.setFormatter(ConsoleFormatter())
        package_logger.addHandler(handler)


def observe_node(name, node, *, include_output=True, stage_callback=None):
    """Log around execution without changing node data, decisions or exceptions."""

    async def execute(state, config=None):
        token = stage_name.set(name)
        started = perf_counter()
        logger.info("stage_started")
        try:
            if stage_callback:
                await stage_callback(name, "started")
            output = (
                await node.ainvoke(state, config=config)
                if hasattr(node, "ainvoke")
                else await node(state)
            )
            if stage_callback:
                await stage_callback(name, "completed")
            validation = output.get("final_validation")
            if validation:
                level = (
                    logging.ERROR
                    if validation["status"] == "FAIL"
                    else (
                        logging.WARNING
                        if validation["status"] == "BEST_EFFORT"
                        else logging.INFO
                    )
                )
                logger.log(
                    level, "final_validation", extra={"status": validation["status"]}
                )
            for role, usage in output.get("metrics", {}).items():
                if isinstance(usage, dict) and (
                    usage.get("error") or usage.get("timeout")
                ):
                    logger.warning("llm_fallback_used", extra={"provider": role})
            logger.info(
                "stage_completed",
                extra={
                    "duration_ms": round((perf_counter() - started) * 1000, 2),
                    "fields": list(output),
                },
            )
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("stage_fields", extra={"fields": list(output)})
            return output
        except Exception as exc:
            if getattr(exc, "expected_pause", False):
                logger.info("stage_paused")
            else:
                logger.exception("stage_failed")
            raise
        finally:
            stage_name.reset(token)

    return wraps(node)(execute) if not hasattr(node, "ainvoke") else execute


def research_output(name, payload):
    """Console counterpart of stage artifacts; records no files."""
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("research_stage", extra={"fields": list(payload)})
