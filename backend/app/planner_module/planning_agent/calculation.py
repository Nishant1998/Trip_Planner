"""Pure Decimal arithmetic and cost ledger; no provider or model access."""
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Category = Literal["main_transport", "local_transport", "accommodation", "meals", "activities", "fees", "reserve"]


class CostItem(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: str = Field(min_length=1, max_length=128)
    category: Category
    lower: Decimal | None = Field(default=None, ge=0)
    upper: Decimal | None = Field(default=None, ge=0)
    quantity: Decimal = Field(gt=0)
    unit: Literal["person", "room_night", "vehicle", "group", "person_day", "item"]
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    basis: Literal["supplier_evidence", "estimate", "planning_allowance", "unknown", "not_applicable"]
    evidence_ids: list[str] = Field(default_factory=list)
    note: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def consistent(self):
        if self.upper is not None and (self.lower is None or self.upper < self.lower):
            raise ValueError("Cost range is reversed or missing its lower bound")
        if self.basis == "unknown" and (self.lower is not None or self.upper is not None):
            raise ValueError("Unknown costs cannot contain invented amounts")
        if self.basis == "supplier_evidence" and not self.evidence_ids:
            raise ValueError("Supplier cost needs evidence references")
        if self.basis == "not_applicable" and (self.lower != 0 or self.upper != 0):
            raise ValueError("Non-applicable category must explicitly record zero")
        return self


class CalculationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    operation: Literal["add", "subtract", "multiply", "divide", "budget"]
    operands: list[Decimal] = Field(default_factory=list, max_length=128)
    items: list[CostItem] = Field(default_factory=list, max_length=256)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    cap: Decimal | None = Field(default=None, ge=0)


def calculate(request: CalculationRequest) -> dict:
    """Caller validates evidence ownership; this engine does not certify sources."""
    if request.operation != "budget":
        values = request.operands
        if not values or (request.operation in {"subtract", "divide"} and len(values) != 2):
            raise ValueError("Invalid operand count")
        value = values[0]
        for operand in values[1:]:
            if request.operation == "add":
                value += operand
            elif request.operation == "subtract":
                value -= operand
            elif request.operation == "multiply":
                value *= operand
            elif operand == 0:
                raise ValueError("Division by zero")
            else:
                value /= operand
        return {"value": str(value), "outcome": "pass"}
    unique = {}
    for item in request.items:
        if item.id in unique and unique[item.id] != item:
            raise ValueError("Conflicting duplicate cost ID")
        if item.currency != request.currency:
            raise ValueError("Currency conversion requires registered FX evidence")
        unique[item.id] = item
    categories = {"main_transport", "local_transport", "accommodation", "meals", "activities", "fees", "reserve"}
    unknown = categories - {item.category for item in unique.values()}
    subtotal = Decimal(0)
    upper = Decimal(0)
    for item in unique.values():
        if item.lower is not None and item.basis != "planning_allowance":
            subtotal += item.lower * item.quantity
        if item.upper is None or item.basis in {"unknown", "planning_allowance"}:
            unknown.add(item.category)
        else:
            upper += item.upper * item.quantity
    outcome = "unknown"
    if request.cap is not None and subtotal > request.cap:
        outcome = "fail"
    elif request.cap is not None and not unknown and upper <= request.cap:
        outcome = "pass"
    money = lambda value: str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    breakdown = {}
    verified_total = Decimal(0)
    estimated_total = Decimal(0)
    for category in sorted(categories):
        rows = [item for item in unique.values() if item.category == category]
        verified = sum((item.lower * item.quantity for item in rows
            if item.basis == "supplier_evidence" and item.lower is not None and item.upper == item.lower), Decimal(0))
        supported = sum((item.lower * item.quantity for item in rows
            if item.lower is not None and item.basis not in {"planning_allowance", "unknown"}), Decimal(0))
        estimated = supported - verified
        category_upper = sum((item.upper * item.quantity for item in rows if item.upper is not None), Decimal(0))
        exact = bool(rows) and all(item.basis == "not_applicable" or (
            item.basis == "supplier_evidence" and item.lower is not None and item.lower == item.upper) for item in rows)
        verified_total += verified
        estimated_total += estimated
        breakdown[category] = {"currency": request.currency, "item_ids": [item.id for item in rows],
            "verified_subtotal": money(verified), "estimated_subtotal": money(estimated),
            "supported_subtotal": money(supported),
            "upper_estimate": None if category in unknown else money(category_upper),
            "status": "unknown" if category in unknown else "known" if exact else "estimated"}
    status = ("known" if not unknown and all(row["status"] == "known" for row in breakdown.values())
              else "partial" if any(item.lower is not None and item.basis not in {"planning_allowance", "unknown"}
                                     for item in unique.values()) else "unknown")
    return {"currency": request.currency, "supported_subtotal": money(subtotal),
            "categories": breakdown, "total_verified": money(verified_total),
            "total_estimated": money(estimated_total), "status": status,
            "upper_estimate": None if unknown else money(upper),
            "unknown_categories": sorted(unknown), "outcome": outcome,
            "item_count": len(unique),
            "remaining_allowance": None if request.cap is None else money(request.cap - subtotal)}
