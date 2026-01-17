"""Validate source-bound planner claims without altering saved research."""
from copy import deepcopy
from decimal import Decimal
from datetime import date
from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .timeline import Window
from .calculation import CostItem
from .accessibility import AccessibilityValue, bind_accessibility


class AdmissionValue(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    audience: Literal["adult", "child"]
    lower: Decimal = Field(ge=0)
    upper: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def ordered_bounds(self):
        if self.upper is not None and self.upper < self.lower:
            raise ValueError("Admission range is reversed")
        return self


class EvidenceClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gap_id: str
    source_id: str
    chunk_id: str
    content_hash: str
    span_start: int = Field(ge=0)
    span_end: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=2000)
    entity_id: str
    field: Literal["visit_duration", "opening_hours", "admission", "expense", "accessibility"]
    applicable_date: date
    fact_id: str
    minutes: int | None = Field(default=None, gt=0, le=1440)
    windows: list[Window] = Field(default_factory=list, max_length=16)
    admission: AdmissionValue | None = None
    accessibility: AccessibilityValue | None = None
    expense: CostItem | None = Field(default=None, description="Source-interpreted cost estimate for one dated scope; never a verified booking. Use a distinct cost ID per stay date or journey scope.")
    expense_step_ids: list[str] = Field(default_factory=list, max_length=64,
        description="Exact travel or meal step IDs covered on applicable_date; empty for an overnight stay bound to entity_id. Uncovered steps remain uncosted.")
    limitations: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def value_matches_field(self):
        if self.field == "accessibility":
            if (self.accessibility is None or self.admission is not None or self.minutes is not None
                    or self.windows or self.expense is not None or self.expense_step_ids):
                raise ValueError("Accessibility claim requires only accessibility values")
            return self
        if self.accessibility is not None:
            raise ValueError("Only accessibility claims can carry accessibility observations")
        if self.field == "expense":
            if (self.expense is None or self.admission is not None or self.minutes is not None
                    or self.windows or self.expense.basis != "estimate"
                    or self.expense.lower is None or self.expense.category not in {
                        "accommodation", "meals", "main_transport", "local_transport"}):
                raise ValueError("Expense requires a source-interpreted estimate only")
            if self.expense.id != self.fact_id or self.expense.evidence_ids != [self.source_id]:
                raise ValueError("Expense identity and evidence must match the claim")
            return self
        if self.expense is not None or self.expense_step_ids:
            raise ValueError("Only expense claims can carry expense values or journey scope")
        if self.field == "admission":
            if self.admission is None or self.minutes is not None or self.windows:
                raise ValueError("Admission claim requires only admission values")
        elif self.admission is not None:
            raise ValueError("Only admission claims can carry prices")
        if self.field == "visit_duration" and (self.minutes is None or self.windows):
            raise ValueError("Duration claim requires minutes only")
        if self.field == "opening_hours" and self.minutes is not None:
            raise ValueError("Opening claim cannot change visit duration")
        return self


def relevant_conflicts(claims, draft):
    """Keep conflicts attached to selected dated visits or included cost items."""
    visits = {(pid, day.date.isoformat()) for day in draft.days for pid in day.selected_poi_ids}
    facts = set()
    for day in draft.days:
        if day.timeline:
            for step in day.timeline.steps:
                if step.kind == "visit":
                    visits.add((step.origin_id, day.date.isoformat()))
                    facts.add(step.id)
    entities = {entity for entity, _ in visits}
    costs = {item.id for item in draft.costs}
    for claim in claims.values():
        if claim.get("status") != "conflict":
            continue
        entity, applicable = claim.get("entity_id"), claim.get("applicable_date")
        if claim.get("field") == "admission":
            # Admission facts are currently keyed by POI/audience, not date.
            audience = (claim.get("admission") or {}).get("audience")
            if entity in entities or f"{audience}_entry:{entity}" in costs:
                yield claim
        elif claim.get("field") == "expense":
            if claim.get("fact_id") in costs:
                yield claim
        elif (claim.get("fact_id") in facts or (entity, applicable) in visits
              or applicable is None and entity in entities):
            yield claim


def apply_claims(utilities, claims, *, draft=None, data=None):
    """Apply a batch atomically; conflicting observations remain unresolved."""
    facts = deepcopy(utilities.step_facts)
    costs = deepcopy(utilities.cost_facts)
    overlay = deepcopy(utilities.claims)
    for claim in claims:
        web = utilities.web
        if web is None:
            raise ValueError("Claim has no retrieved source")
        gap = web.gaps.get(claim.gap_id, {})
        if (gap.get("entity_id"), gap.get("field")) != (claim.entity_id, claim.field):
            raise ValueError("Claim targets another gap")
        source = web.registry.read_target(claim.source_id, entity_id=claim.entity_id, field=claim.field)
        observed = web.cache.get(f"{claim.gap_id}\0{claim.source_id}", {})
        excerpts = [item for item in observed.get("items", []) if item.get("source_id") == source.id
                    and item.get("chunk_id") == claim.chunk_id and item.get("content_hash") == claim.content_hash
                    and item.get("basis") == "extracted_excerpt"]
        if observed.get("status") != "ok" or len(excerpts) != 1:
            raise ValueError("Claim requires a matching extracted page, not a search snippet")
        excerpt = excerpts[0]
        text = excerpt["text"]
        if (excerpt.get("truncated") or sha256(text.encode()).hexdigest() != claim.content_hash
                or claim.span_end > len(text) or text[claim.span_start:claim.span_end] != claim.quote):
            raise ValueError("Claim span is missing, changed or truncated")
        fact = facts.get(claim.fact_id)
        if claim.field == "accessibility":
            accessibility_scope_hash = bind_accessibility(claim, draft, data)
        elif claim.field == "expense":
            from .expense_scope import validate_expense_scope
            expense_scope_hash = validate_expense_scope(claim, draft)
        elif (fact is None or fact.get("kind") != "visit" or fact.get("origin_id") != claim.entity_id
                or fact.get("applicable_date") != claim.applicable_date.isoformat()):
            raise ValueError("Claim does not match a registered dated visit")
        raw = claim.model_dump(mode="json")
        key = sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
        if key in overlay:
            continue
        if claim.field == "accessibility":
            conflict = False
        elif claim.field == "expense":
            item = claim.expense.model_dump(mode="json")
            existing = costs.get(claim.fact_id)
            conflict = (existing is not None and existing != item or any(
                prior.get("field") == "expense" and prior.get("fact_id") == claim.fact_id
                and prior.get("expense_scope_hash") != expense_scope_hash for prior in overlay.values()))
            if not conflict:
                costs[claim.fact_id] = item
        elif claim.field == "visit_duration":
            if fact.get("minimum_minutes") and claim.minutes < fact["minimum_minutes"]:
                conflict = True
            else:
                conflict = fact.get("minutes") is not None and fact["minutes"] != claim.minutes
            if not conflict:
                fact.update(minutes=claim.minutes, basis="estimate")
        elif claim.field == "opening_hours":
            for window in claim.windows:
                if window.start.date() != claim.applicable_date or window.end <= window.start:
                    raise ValueError("Opening window must match the cited visit date")
            windows = [window.model_dump(mode="json") for window in claim.windows]
            conflict = fact.get("availability_known", False) and fact.get("windows") != windows
            if not conflict:
                fact.update(windows=windows, availability_known=True)
        else:
            value = claim.admission
            cost_id = f"{value.audience}_entry:{claim.entity_id}"
            existing = costs.get(cost_id)
            if existing is None or existing.get("unit") != "person" or existing.get("category") != "activities":
                raise ValueError("Admission requires a registered party cost item")
            if value.currency != existing.get("currency"):
                raise ValueError("Admission currency does not match the registered cost")
            conflict = (existing.get("lower") is not None and Decimal(existing["lower"]) != value.lower
                or existing.get("upper") is not None and
                (value.upper is None or Decimal(existing["upper"]) != value.upper)
                or any(prior.get("field") == "admission" and prior.get("entity_id") == claim.entity_id
                    and prior.get("admission", {}).get("audience") == value.audience
                    and prior.get("applicable_date") != claim.applicable_date.isoformat()
                    for prior in overlay.values()))
            if not conflict:
                item = CostItem(id=cost_id, category="activities", lower=value.lower, upper=value.upper,
                    quantity=existing["quantity"], unit="person", currency=value.currency, basis="estimate",
                    evidence_ids=list(dict.fromkeys([*existing.get("evidence_ids", []), claim.source_id])),
                    note=f"Model-interpreted admission excerpt for {claim.applicable_date}: {claim.limitations}"[:1000])
                costs[cost_id] = item.model_dump(mode="json")
        overlay[key] = {**raw, "status": "conflict" if conflict else "accepted",
                        "basis": "model_interpreted_excerpt", "retrieved_at": observed.get("retrieved_at"),
                        "url": source.url, "independently_verified": False}
        if claim.field == "expense":
            overlay[key]["expense_scope_hash"] = expense_scope_hash
        if claim.field == "accessibility":
            overlay[key]["accessibility_scope_hash"] = accessibility_scope_hash
    changed = overlay != utilities.claims
    utilities.step_facts, utilities.cost_facts, utilities.claims = facts, costs, overlay
    return changed
