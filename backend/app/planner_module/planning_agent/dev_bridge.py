"""Explicit local model substitution; never imports or dispatches an API client."""
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import tempfile

from pydantic import BaseModel, ConfigDict, Field

from .contracts import fingerprint
from .protocol import ModelReply, ModelRequest
from .wire import WireReply


class BridgePending(Exception):
    expected_pause = True

    def __init__(self, request_path: Path, response_path: Path):
        self.request_path, self.response_path = request_path, response_path
        super().__init__("Development model response is pending")


class BridgeClock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_started: datetime
    waiting_since: datetime | None = None
    paused_seconds: float = Field(default=0, ge=0)
    session_cap_seconds: float = Field(default=7200, gt=0, le=86400)

    def check(self, now: datetime):
        if now.tzinfo is None or self.session_started.tzinfo is None:
            raise ValueError("Bridge clock requires timezone-aware timestamps")
        elapsed = (now - self.session_started).total_seconds()
        if elapsed < 0 or elapsed >= self.session_cap_seconds:
            raise TimeoutError("Development bridge session expired or clock regressed")

    def begin_wait(self, now: datetime):
        self.check(now)
        if self.waiting_since is None:
            self.waiting_since = now

    def finish_wait(self, now: datetime, deadline: datetime):
        self.check(now)
        if self.waiting_since is None:
            return deadline
        elapsed = (now - self.waiting_since).total_seconds()
        if elapsed < 0:
            raise ValueError("Bridge clock regressed")
        self.paused_seconds += elapsed
        self.waiting_since = None
        return deadline + timedelta(seconds=elapsed)


def atomic_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".planner-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class DevelopmentBridge:
    def __init__(self, directory: Path, *, mode: str, enabled: bool = False):
        if not enabled or mode not in {"development", "test"}:
            raise ValueError("Development bridge requires explicit development/test activation")
        self.directory = Path(directory).resolve()

    def paths(self, request: ModelRequest):
        # Hash identities rather than treating external IDs as filesystem paths.
        folder = self.directory / fingerprint({"run": request.run_id, "owner": request.owner_scope})
        stem = fingerprint({"turn": request.turn_id})
        return folder / f"{stem}.request.json", folder / f"{stem}.response.json"

    async def invoke(self, request: ModelRequest) -> ModelReply:
        request_path, response_path = self.paths(request)
        reply_type = WireReply if request.context.get("update_format") == "entries_v1" else ModelReply
        export = {"request": request.model_dump(mode="json"),
                  "request_fingerprint": request.request_fingerprint,
                  "response_schema": reply_type.model_json_schema(),
                  "backend": "dev_codex_bridge", "application_llm_api_requests": 0}
        if request_path.exists():
            saved = json.loads(request_path.read_text(encoding="utf-8"))
            if saved != export:
                raise ValueError("Existing bridge request differs; refusing overwrite")
        else:
            atomic_json(request_path, export)
        if not response_path.exists():
            raise BridgePending(request_path, response_path)
        if response_path.stat().st_size > 1_000_000:
            raise ValueError("Bridge response exceeds size limit")
        reply = reply_type.model_validate_json(response_path.read_text(encoding="utf-8"))
        if isinstance(reply, WireReply):
            reply = reply.to_internal()
        reply.match(request)
        return reply
