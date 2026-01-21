"""Checkpoint-before-execution utility dispatch for a single owned planner run."""
import asyncio
from copy import deepcopy
from typing import Awaitable, Callable, get_args

from pydantic import ValidationError

from .protocol import ExchangeLedger, UtilityName
from .resources import ResourceGuard, ResourceLimit
from .route_tool import RouteDispatchDenied


class UtilityDispatcher:
    def __init__(self, *, ledger: ExchangeLedger, guard: ResourceGuard,
                 handlers: dict[str, Callable[[dict], Awaitable[dict]]],
                 checkpoint: Callable[[dict], Awaitable[None]]):
        if set(handlers) != set(get_args(UtilityName)):
            raise ValueError("Exactly the six planner utilities must be bound")
        self.ledger = ledger
        self.guard = guard
        self.handlers = handlers
        self.checkpoint = checkpoint
        self.lock = asyncio.Lock()

    async def _save(self):
        await self.checkpoint({"exchange": self.ledger.model_dump(mode="json"),
                               "resources": self.guard.ledger.model_dump(mode="json")})

    async def execute(self, call_ids: list[str], *, owner_scope: str, run_id: str):
        # Serialize one model batch: deterministic observations and no duplicate
        # side effects when two resumes arrive together. Providers may parallelize
        # independent route pairs inside their own guarded utility.
        async with self.lock:
            if (owner_scope, run_id) != (self.ledger.owner_scope, self.ledger.run_id):
                raise ValueError("Wrong planner owner or run")
            if len(call_ids) != len(set(call_ids)) or any(i not in self.ledger.calls for i in call_ids):
                raise ValueError("Unknown or duplicate call IDs")
            observations = []
            for call_id in call_ids:
                record = self.ledger.calls[call_id]
                if record.status == "started":
                    # A previous interrupted process may have contacted a paid
                    # provider; do not assume failure means safe re-dispatch.
                    self.ledger.complete_call(call_id, {
                        "status": "unknown", "reason": "interrupted_attempt_not_repeated"})
                    await self._save()
                elif record.status == "pending":
                    try:
                        await self.guard.reserve("utility")
                    except ResourceLimit as exc:
                        self.ledger.start_call(call_id)
                        self.ledger.complete_call(call_id, {"status": "limited", "reason": str(exc)})
                        await self._save()
                    else:
                        self.ledger.start_call(call_id)
                        # A failed checkpoint must stop before any handler runs.
                        await self._save()
                        try:
                            async with asyncio.timeout(self.guard.remaining()):
                                result = await self.handlers[record.call.name](deepcopy(record.call.arguments))
                            if not isinstance(result, dict):
                                raise TypeError("Utility must return a structured observation")
                        except RouteDispatchDenied as exc:
                            result = {"status": "limited" if exc.reason == "route_resource_limit" else "unknown",
                                      "reason": exc.reason}
                        except ResourceLimit as exc:
                            result = {"status": "limited", "reason": str(exc)}
                        except ValidationError:
                            result = {"status": "rejected", "reason": "invalid_arguments"}
                        except ValueError:
                            result = {"status": "rejected", "reason": "invalid_input_or_evidence"}
                        except TimeoutError:
                            result = {"status": "timeout", "reason": "working_deadline"}
                        # Cancellation and programming faults propagate; persisted
                        # started state prevents accidental repeat on recovery.
                        self.ledger.complete_call(call_id, result)
                        await self._save()
                observations.append({"call_id": call_id, "name": record.call.name,
                                     "result": deepcopy(record.result)})
            return observations
