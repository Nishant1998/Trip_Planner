"""Bounded directed-cost ordering; no selection, provider or model calls."""
from time import monotonic
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DirectedCost(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    origin_id: str
    destination_id: str
    minutes: float | None = Field(default=None, ge=0)
    route_fact_id: str | None = None


class OrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_ids: list[str] = Field(max_length=64)
    start_id: str
    end_id: str
    departure_date: date | None = None
    routes: list[DirectedCost] = Field(default_factory=list, max_length=4096)
    fixed_subsequences: list[list[str]] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def identities(self):
        if len(set(self.selected_ids)) != len(self.selected_ids):
            raise ValueError("Selected visit instances must be distinct")
        for sequence in self.fixed_subsequences:
            if len(set(sequence)) != len(sequence) or not set(sequence) <= set(self.selected_ids):
                raise ValueError("Fixed subsequence must contain distinct selected IDs")
        return self


def optimize_stop_order(request: OrderRequest, *, max_candidates: int = 200,
                        max_seconds: float = 2.0, clock=monotonic) -> dict:
    """Preserve selected identities; travel-cost improvement is not timeline proof."""
    if not 1 <= max_candidates <= 200 or not 0 < max_seconds <= 2:
        raise ValueError("Ordering limits exceed the server policy")
    deadline = clock() + max_seconds
    costs = {}
    for route in request.routes:
        pair = (route.origin_id, route.destination_id)
        if pair in costs and costs[pair] != route.minutes:
            raise ValueError("Conflicting directed route costs")
        costs[pair] = route.minutes

    def permitted(order):
        positions = {identity: index for index, identity in enumerate(order)}
        return all([positions[item] for item in sequence] == sorted(positions[item] for item in sequence)
                   for sequence in request.fixed_subsequences)

    def evaluate(order):
        path = [request.start_id, *order, request.end_id]
        gaps, total = [], 0.0
        for left, right in zip(path, path[1:]):
            minutes = 0 if left == right else costs.get((left, right))
            if minutes is None:
                gaps.append([left, right])
            else:
                total += minutes
        return (len(gaps), total), gaps

    # Stable topological seed preserves explicit subsequences without freezing
    # the arbitrary order of a researched city/POI list.
    before = {item: set() for item in request.selected_ids}
    for sequence in request.fixed_subsequences:
        for left, right in zip(sequence, sequence[1:]):
            before[right].add(left)
    best = []
    while len(best) < len(request.selected_ids):
        available = [item for item in request.selected_ids if item not in best and before[item] <= set(best)]
        if not available:
            return {"ordered_ids": request.selected_ids, "outcome": "fail",
                    "reason": "contradictory_fixed_subsequences", "evaluations": 0}
        best.append(available[0])
    initial, _ = evaluate(best)
    score, gaps = evaluate(best)
    evaluations = 1
    improved = True
    while improved and evaluations < max_candidates and clock() < deadline:
        improved = False
        for left in range(len(best)):
            for right in range(len(best)):
                if evaluations >= max_candidates or clock() >= deadline:
                    break
                if left == right:
                    continue
                candidate = best.copy()
                candidate.insert(right, candidate.pop(left))
                if not permitted(candidate):
                    continue
                candidate_score, candidate_gaps = evaluate(candidate)
                evaluations += 1
                if candidate_score < score:
                    best, score, gaps = candidate, candidate_score, candidate_gaps
                    improved = True
            if evaluations >= max_candidates or clock() >= deadline:
                break
    return {"ordered_ids": best, "start_id": request.start_id, "end_id": request.end_id,
            "outcome": "unknown" if gaps else "pass", "unresolved_edges": gaps,
            "travel_minutes": None if gaps else score[1], "known_minutes_subtotal": score[1],
            "initial_known_minutes_subtotal": initial[1], "evaluations": evaluations,
            "timeline_checked": False, "globally_optimal": False,
            "basis": "directed supplied route costs; separate dated timeline check required"}
