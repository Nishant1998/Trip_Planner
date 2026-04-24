"""Budget allocation advice, kept separate from source-backed cost totals."""

from .contracts import BudgetGuidance


def spending_targets(request):
    if not request.budget or not request.currency:
        return None
    nights = (request.end_date - request.start_date).days
    if nights:
        intercity = request.origin not in request.destinations or len(request.destinations) > 1
        weights = {"accommodation": 30, "transport": 25 if intercity else 20,
                   "meals": 15, "admissions": 15, "contingency": 15 if intercity else 20}
    else:
        weights = {"transport": 30, "meals": 20, "admissions": 20, "contingency": 30}
    targets = {category: round(request.budget * weight / 100, 2)
               for category, weight in weights.items()}
    targets["contingency"] = round(request.budget - sum(
        amount for category, amount in targets.items() if category != "contingency"), 2)
    return BudgetGuidance(
        currency=request.currency, budget=request.budget,
        adults=request.adults, children=request.children, nights=nights,
        category_limits=targets,
        basis="Suggested spending limits for the entire trip and all travelers, not quotes or market estimates. "
              "Start with these allocations and adjust after checking accommodation, transport, tickets and meals. "
              "No expenses are assumed prepaid; actual unpriced costs remain unknown.",
    )
