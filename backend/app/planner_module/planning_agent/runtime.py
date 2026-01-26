"""Guarded node implementations shared by API and development model backends."""
import asyncio
from copy import deepcopy
from datetime import UTC, datetime

from ..models.planning import PlannerInput
from .contracts import VersionedDraft, fingerprint
from .context import build_guarded_request
from .dispatch import UtilityDispatcher
from .dev_bridge import BridgeClock, BridgePending, DevelopmentBridge
from .api_backend import PlannerAPIBackend
from .milestone_checks import check_milestone
from .stages import AgentProgress, AgentRole, MilestoneReceipt, PlanningTask
from .presentation_guard import presentation_options, presentation_catalog, validate_presentation
from .claims import apply_claims
from .constraints import build_constraint_ledger
from .protocol import ExchangeLedger, ModelReply
from .resources import ResourceGuard, ResourceLedger, ResourceLimit
from .workflow import NODE_NAMES, build_guarded_workflow


class GuardedPlannerRuntime:
    def __init__(self, *, data: PlannerInput, session, backend, utilities,
                 validate_details, finalize, explicit_order=(), resource_ledger=None):
        self.data = data.model_copy(deep=True)
        self.session, self.backend, self.utilities = session, backend, utilities
        self.validate_details, self.finalizer = validate_details, finalize
        self.explicit_order = tuple(explicit_order)
        self.constraints = build_constraint_ledger(self.data, explicit_order=self.explicit_order)
        saved = session.state
        digest = fingerprint(data)
        if session.identity["input_hash"] != digest:
            raise ValueError("Planner input differs from checkpoint input")
        self.versioned = VersionedDraft.model_validate(saved.get("versioned") or {"input_fingerprint": digest})
        self.exchange = ExchangeLedger.model_validate(saved.get("exchange") or {
            "run_id": session.identity["run_id"], "owner_scope": session.identity["owner_scope"]})
        self.guard = ResourceGuard(ResourceLedger.model_validate(saved["resources"]) if "resources" in saved else resource_ledger or ResourceLedger.start())
        if utilities.routes is not None:
            utilities.routes.guard = self.guard
            utilities.routes.checkpoint = self.save
        if utilities.web is not None:
            utilities.web.guard = self.guard
            utilities.web.checkpoint = self.save
        if saved.get("utilities"):
            utilities.restore(saved["utilities"])
        self.feedback = deepcopy(saved.get("feedback", {}))
        self.progress = AgentProgress.model_validate(saved.get("agent_progress") or {})
        self.review_result = deepcopy(saved.get("review_result"))
        self.reviewed_draft_hash = saved.get("reviewed_draft_hash")
        self.repairs = saved.get("repairs", 0)
        self.repair_target_phase = saved.get("repair_target_phase")
        self.stop_reason = saved.get("stop_reason")
        if saved.get("versioned") and not saved.get("agent_progress") and self.stop_reason is None:
            self.stop_reason = "legacy_checkpoint_requires_new_run"
        self.model_in_flight = saved.get("model_in_flight", False)
        if self.model_in_flight and self.stop_reason is None and isinstance(backend, PlannerAPIBackend):
            self.stop_reason = "model_outcome_unknown_after_restart"
        self.reply = saved.get("reply")
        self.candidates = deepcopy(saved.get("candidates", []))
        self.best_candidate = deepcopy(saved.get("best_candidate"))
        self.best_score = tuple(saved["best_score"]) if saved.get("best_score") is not None else None
        self.bridge_clock = None
        if isinstance(backend, DevelopmentBridge):
            self.bridge_clock = (BridgeClock.model_validate(saved["bridge_clock"]) if saved.get("bridge_clock")
                                 else BridgeClock(session_started=datetime.now(UTC)))
        self.finished_artifact = session.store.load_artifact(**session.identity)
        self.artifact = deepcopy(saved.get("final_artifact"))
        self.presentation_in_flight = saved.get("presentation_in_flight", False)
        if saved.get("model_backend"):
            if not isinstance(backend, PlannerAPIBackend):
                raise ValueError("Cannot resume API state through another backend")
            backend.restore(saved["model_backend"])
        self.dispatcher = UtilityDispatcher(ledger=self.exchange, guard=self.guard,
            handlers=utilities.handlers, checkpoint=self._dispatch_save)

    async def save(self):
        if self.finished_artifact is not None:
            return
        await self.session.save({"versioned": self.versioned.model_dump(mode="json"),
            "exchange": self.exchange.model_dump(mode="json"), "resources": self.guard.ledger.model_dump(mode="json"),
            "feedback": self.feedback, "repairs": self.repairs, "stop_reason": self.stop_reason,
            "agent_progress": self.progress.model_dump(mode="json"), "review_result": self.review_result,
            "reviewed_draft_hash": self.reviewed_draft_hash,
            "model_in_flight": self.model_in_flight,
            "presentation_in_flight": self.presentation_in_flight,
            "repair_target_phase": self.repair_target_phase,
            "reply": self.reply, "candidates": self.candidates,
            "best_candidate": self.best_candidate, "best_score": self.best_score,
            "utilities": self.utilities.snapshot(),
            "model_backend": self.backend.snapshot() if isinstance(self.backend, PlannerAPIBackend) else None,
            "bridge_clock": self.bridge_clock.model_dump(mode="json") if self.bridge_clock else None})

    async def _dispatch_save(self, changes):
        await self.save()

    def graph(self, *, checkpointer=None):
        return build_guarded_workflow({name: getattr(self, name if name != "guard" else "work_guard")
                                       for name in NODE_NAMES}, checkpointer=checkpointer).with_config(recursion_limit=100)

    async def prepare_state(self, state):
        if self.finished_artifact is not None:
            self.stop_reason = "already_finalized"
        elif self.bridge_clock:
            try:
                self.guard.ledger.deadline = self.bridge_clock.finish_wait(datetime.now(UTC), self.guard.ledger.deadline)
            except TimeoutError:
                self.stop_reason = "bridge_session_expired"
        await self.save()
        return {"input": self.data, "planner_state": {"phase": self.versioned.phase}}

    async def work_guard(self, state):
        if self.artifact is not None:
            return {"planner_work_allowed": False}
        try:
            self.guard.remaining()
            if self.guard.ledger.attempts.get("model", 0) >= self.guard.limits["model"] and not (self.reply or self.exchange.pending):
                self.stop_reason = "model_quota"
        except ResourceLimit as exc:
            self.stop_reason = str(exc)
        await self.save()
        return {"planner_work_allowed": self.stop_reason is None,
                "planner_agent_role": self.progress.stage.agent.value}

    async def planning_agent(self, state):
        if self.progress.stage.agent != AgentRole.PLANNING:
            raise ValueError("Planning node cannot invoke another agent")
        return await self._invoke_agent(state)

    async def review_agent(self, state):
        if self.progress.stage.agent != AgentRole.REVIEW:
            raise ValueError("Review node requires completed planning milestones")
        return await self._invoke_agent(state)

    async def _invoke_agent(self, state):
        if self.stop_reason is not None:
            return {"planner_reply": None}
        if self.reply is not None:
            return {"planner_reply": self.reply}
        if self.exchange.pending is None:
            try:
                await self.guard.reserve("model")
            except ResourceLimit as exc:
                self.stop_reason = str(exc)
                await self.save()
                return {"planner_reply": None}
            observations = [{"call_id": key, "name": value.call.name, "result": value.result}
                            for key, value in self.exchange.calls.items() if value.status == "completed"]
            payload = self.data.model_dump(mode="json")
            self.exchange.pending = build_guarded_request(
                **{key: self.session.identity[key] for key in ("run_id", "owner_scope")},
                turn_id=str(self.guard.ledger.attempts["model"]), versioned=self.versioned,
                confirmed={key: value for key, value in payload.items() if key not in {"evidence", "pois", "stops", "source_report_path"}},
                catalog={"stops": payload["stops"], "pois": payload["pois"], "anchors": self.utilities.anchor_catalog},
                constraints={"explicit_order": self.explicit_order, "ledger": self.constraints.model_dump(mode="json")}, evidence={"research": payload["evidence"],
                    "overlay": self.utilities.claims,
                    "step_facts": self.utilities.step_facts, "cost_facts": self.utilities.cost_facts,
                    "fx_facts": self.utilities.fx_facts,
                    "web_gaps": self.utilities.web.gaps if self.utilities.web else {},
                    "web_sources": [source.model_dump(mode="json") for source in self.utilities.web.registry.sources.values()] if self.utilities.web else []},
                feedback={**self.feedback, "repair_target_phase": self.repair_target_phase}, budgets={"remaining_seconds": self.guard.remaining(),
                    "model_attempts": self.guard.ledger.attempts["model"], "repairs_used": self.repairs,
                    "limits": self.guard.ledger.limits.model_dump(mode="json"),
                    "attempts": dict(self.guard.ledger.attempts)},
                tool_schemas=self.utilities.schemas(), observations=observations, stage=self.progress.stage)
            await self.save()
        request = self.exchange.pending
        if isinstance(self.backend, PlannerAPIBackend):
            self.model_in_flight = True
            await self.save()
        try:
            async with asyncio.timeout(min(self.guard.ledger.limits.model_seconds, self.guard.remaining())):
                reply = await self.backend.invoke(request)
            reply = ModelReply.model_validate(reply)
            self.exchange.accept(reply)
            self.model_in_flight = False
        except asyncio.CancelledError:
            self.guard.ledger.cancelled = True
            self.stop_reason = "cancelled"
            await asyncio.shield(self.save())
            raise
        except BridgePending:
            if self.bridge_clock is None:
                raise RuntimeError("Bridge wait is forbidden for the production backend")
            self.bridge_clock.begin_wait(datetime.now(UTC))
            await self.save()
            raise
        except ResourceLimit as exc:
            self.model_in_flight = False
            self.stop_reason = str(exc)
            self.exchange.pending = None
            await self.save()
            return {"planner_reply": None}
        except (TimeoutError, ValueError) as exc:
            self.model_in_flight = False
            self.feedback = {"ready": False, "model_error": type(exc).__name__}
            self.exchange.pending = None
            await self.save()
            return {"planner_reply": None}
        self.reply = reply.model_dump(mode="json")
        await self.save()
        return {"planner_reply": self.reply}

    async def execute_tools(self, state):
        if self.reply is None:
            # Application state may have committed before the graph cursor did.
            return {"planner_reply": None}
        reply = ModelReply.model_validate(self.reply)
        await self.dispatcher.execute([call.id for call in reply.calls],
            owner_scope=self.exchange.owner_scope, run_id=self.exchange.run_id)
        self.reply = None
        await self.save()
        return {"planner_reply": None}

    async def commit_and_check(self, state):
        if self.reply is None:
            return {"planner_reply": None, "planner_finalize": self.stop_reason is not None}
        reply = ModelReply.model_validate(self.reply)
        if reply.stage != self.progress.stage:
            raise ValueError("Reply does not belong to the active agent task")
        reviewing = self.progress.stage.agent == AgentRole.REVIEW
        update = reply.review.corrected_candidate if reviewing else reply.update
        if reviewing:
            valid_days = set(range(1, self.data.duration_days + 1))
            valid_requirements = {record.id for record in self.constraints.records} | {row.id for row in self.data.requirements}
            valid_evidence = {row["id"] for row in self.data.evidence if row.get("id")}
            valid_evidence.update(row["source_id"] for row in self.utilities.claims.values() if row.get("source_id"))
            if self.utilities.web is not None:
                valid_evidence.update(self.utilities.web.registry.sources)
            if any(not set(finding.day_numbers) <= valid_days
                   or not set(finding.entity_ids) <= self.utilities.entity_ids
                   or not set(finding.requirement_ids) <= valid_requirements
                   or not set(finding.evidence_ids) <= valid_evidence for finding in reply.review.findings):
                self.feedback = {"ready": False, "update_error": "review_references_outside_run_scope"}
                self.reply = None
                await self.save()
                return {"planner_reply": None, "planner_finalize": False}
        if reviewing and update is not None and self.repairs >= self.guard.ledger.limits.repairs:
            if reviewing:
                # Keep the reviewer diagnosis even when the correction allowance
                # is exhausted; only the proposed mutation is rejected.
                self.review_result = reply.review.model_dump(mode="json")
                self.reviewed_draft_hash = fingerprint(self.versioned.draft)
                self.feedback["review"] = self.review_result
            self.stop_reason = "repair_limit"
            self.reply = None
            await self.save()
            return {"planner_reply": None, "planner_finalize": True}
        try:
            candidate = self.versioned.apply(update) if update else self.versioned.model_copy(deep=True)
            evidence_changed = apply_claims(self.utilities, update.evidence_claims, draft=candidate.draft, data=self.data) if update else False
            if evidence_changed:
                candidate.evidence_revision += 1
        except ValueError:
            self.feedback = {"ready": False, "update_error": "stale_or_invalid_update"}
            self.reply = None
            await self.save()
            return {"planner_reply": None, "planner_finalize": False}
        task = PlanningTask.HANDOFF if reviewing else self.progress.stage.task
        check = check_milestone(candidate.draft, self.data, task=task, explicit_order=self.explicit_order,
                            registered_anchors=self.utilities.entity_ids,
                            anchor_bases={item["id"]: item["base_id"] for item in self.utilities.anchor_catalog})
        self.feedback = check.model_dump(mode="json")
        if not reviewing and not check.can_advance:
            # Milestones are accepted checkpoints, not a log of every proposal.
            # Keep the last accepted plan authoritative and return the findings
            # so the same task can retry without contaminating later work.
            # Independently validated retrieved claims remain reusable, and the
            # authoritative plan adopts their evidence revision without adopting
            # the rejected draft.
            if evidence_changed:
                self.versioned.evidence_revision = candidate.evidence_revision
            self.reply = None
            await self.save()
            return {"planner_reply": None, "planner_finalize": False}
        self.versioned = candidate
        self.candidates = (self.candidates + [candidate.model_dump(mode="json")])[-4:]
        if reviewing:
            if update is not None:
                self.repairs += 1
            self.review_result = reply.review.model_dump(mode="json")
            self.reviewed_draft_hash = fingerprint(candidate.draft)
            self.utilities.constraint_review = {"draft_hash": self.reviewed_draft_hash,
                "input_fingerprint": candidate.input_fingerprint, "evidence_revision": candidate.evidence_revision,
                "assessments": [row.model_dump(mode="json") for row in reply.review.hard_constraints]}
            validation = await self.validate_details(candidate, self.data)
            self.feedback["validation"] = validation
            self.feedback["review"] = self.review_result
            if update is not None:
                # A correction is never self-certifying. Persist it, then send
                # the changed candidate through this same independent agent
                # boundary once more before the Python delivery gate.
                self.feedback["correction_notes"] = update.rationale
            elif check.can_advance and validation.get("ready") is True and reply.review.outcome != "unresolved":
                self.stop_reason = "accepted"
            else:
                # The repair owner has explicitly reported that no safe factual
                # correction is available. Repeating the unchanged review would
                # only spend the shared model budget.
                self.stop_reason = ("repair_limit" if self.repairs >= self.guard.ledger.limits.repairs
                                    else "review_unresolved_no_correction")
        elif check.can_advance:
            if task in {PlanningTask.SCHEDULE, PlanningTask.BUDGET, PlanningTask.HANDOFF}:
                self.feedback["validation"] = await self.validate_details(candidate, self.data)
            self.progress.complete_milestone(MilestoneReceipt(task=task,
                plan_version=candidate.plan_version, evidence_revision=candidate.evidence_revision,
                draft_hash=fingerprint(candidate.draft),
                unresolved_codes=[issue.code for issue in check.issues]), checked=True)
            self.versioned.phase = "repair" if self.progress.stage.agent == AgentRole.REVIEW else "city_details"
        validation = self.feedback.get("validation")
        if validation is not None:
            score = (sum(row.get("outcome") == "fail" for row in validation.get("issues", [])),
                     sum(row.get("outcome") == "unknown" for row in validation.get("issues", [])),
                     -len(validation.get("timelines", {})), -candidate.plan_version)
            if self.best_score is None or score < self.best_score:
                self.best_candidate = candidate.model_dump(mode="json")
                self.best_score = score
        self.reply = None
        await self.save()
        return {"planner_reply": None, "planner_finalize": self.stop_reason is not None}

    async def finalize_result(self, state):
        if self.finished_artifact is not None:
            self.artifact = self.finished_artifact
            return {"planner_finalized": True}
        if self.artifact is not None:
            return {"planner_finalized": True}
        history = [*self.candidates, *([self.best_candidate] if self.best_candidate is not None else [])]
        self.artifact = await self.finalizer(self.versioned, self.data, history, self.stop_reason)
        self.artifact["agent_progress"] = self.progress.model_dump(mode="json")
        self.artifact["review"] = {"draft_hash": self.reviewed_draft_hash, "result": self.review_result}
        reviewed = (self.review_result is not None and self.review_result["outcome"] != "unresolved"
            and self.reviewed_draft_hash == fingerprint(self.artifact.get("draft", {})))
        if self.artifact.get("status") == "READY" and not reviewed:
            self.artifact["status"] = "DRAFT_NEEDS_CONFIRMATION"
            self.artifact["validation"]["ready"] = False
            self.artifact["validation"]["issues"].append({"code": "REVIEW_INCOMPLETE", "outcome": "unknown",
                "message": "The selected itinerary has not completed independent review without unresolved findings."})
        await self.session.save({"final_artifact": self.artifact})
        return {"planner_finalized": True}

    async def presentation_agent(self, state):
        if self.finished_artifact is not None or self.artifact.get("presentation") is not None:
            return {}
        fallback = presentation_options(self.artifact, self.data)
        reason = "not_eligible"
        try:
            if self.presentation_in_flight:
                raise ValueError("Presentation outcome unknown after restart")
            if self.progress.stage.agent not in {AgentRole.REVIEW, AgentRole.PRESENTATION}:
                raise ValueError("Planning milestones incomplete")
            if not self.artifact.get("validation") or not self.artifact.get("draft", {}).get("days"):
                raise ValueError("No checked itinerary to present")
            if self.progress.stage.agent == AgentRole.REVIEW:
                self.progress.begin_presentation(final_gate_checked=True)
            if self.exchange.pending is None:
                await self.guard.reserve("model")
                selected = VersionedDraft(input_fingerprint=self.versioned.input_fingerprint,
                    plan_version=self.artifact.get("plan_version", self.versioned.plan_version),
                    evidence_revision=self.artifact.get("evidence_revision", self.versioned.evidence_revision),
                    phase="city_details", draft=self.artifact["draft"])
                self.exchange.pending = build_guarded_request(
                    **{key: self.session.identity[key] for key in ("run_id", "owner_scope")},
                    turn_id=str(self.guard.ledger.attempts["model"]), versioned=selected,
                    confirmed={}, catalog={}, constraints={}, evidence={}, feedback={},
                    budgets={"remaining_seconds": self.guard.remaining(), "attempts": dict(self.guard.ledger.attempts)},
                    tool_schemas=[], observations=[], stage=self.progress.stage,
                    checked_output={"itinerary": {key: self.artifact[key] for key in ("draft", "validation", "status")}, "allowed_copy": fallback,
                        "copy_catalog": presentation_catalog(self.artifact, self.data),
                        "text_policy": "Choose an introduction from copy_catalog. For each day choose and order catalog sections including required_section; set summary_parts and join them with single spaces for summary. Or keep the default day's summary with empty summary_parts. Select up to 12 useful practical_notes verbatim. Preserve day order, entity IDs, status and uncertainty; do not invent or paraphrase facts."})
                await self.save()
            request = self.exchange.pending
            if request.stage != self.progress.stage:
                raise ValueError("Pending request is not a presentation request")
            if isinstance(self.backend, PlannerAPIBackend):
                self.presentation_in_flight = True
                await self.save()
            async with asyncio.timeout(min(self.guard.ledger.limits.model_seconds, self.guard.remaining())):
                reply = ModelReply.model_validate(await self.backend.invoke(request))
            self.exchange.accept(reply)
            copy = validate_presentation(reply.presentation, self.artifact, self.data)
            reason = "validated"
        except BridgePending:
            self.bridge_clock.begin_wait(datetime.now(UTC))
            await self.save()
            raise
        except (ValueError, TimeoutError, ResourceLimit) as exc:
            copy = fallback
            reason = type(exc).__name__
        self.presentation_in_flight = False
        self.exchange.pending = None
        self.artifact["presentation"] = {"source": "agent" if reason == "validated" else "deterministic",
            "reason": reason, "text": copy}
        await self.save()
        await self.session.save({"final_artifact": self.artifact})
        return {}

    async def persist_and_present(self, state):
        if self.finished_artifact is not None:
            self.artifact = deepcopy(self.finished_artifact)
            return {"planner_persisted": True, "planner_state": {"artifact": self.artifact}}
        self.guard.ledger.finalized = True
        await self.save()
        self.session.store.finalize(**self.session.identity, expected_revision=self.session.revision,
                                    artifact=self.artifact)
        self.finished_artifact = deepcopy(self.artifact)
        return {"planner_persisted": True, "planner_state": {"artifact": self.artifact}}
