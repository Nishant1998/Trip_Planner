"""One local calculator entry point; not registered in the planner.

calculate({"expression": "rate * rooms * nights", "variables":
           {"rate": "2000", "rooms": 2, "nights": 3}, "decimal_places": 2})
returns {"status": "ok", "result": "12000.00"}. Use equations={name: formula}
for a batch; names may reference other equations in any order. No eval/exec.
Numbers return as decimal strings, comparisons as booleans. Use compatible
currencies/units and supplied facts: the calculator cannot verify price scope.
Unknowns never become zero. Failed batches return no partial results.
"""

import ast
import keyword
import operator
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, DecimalException, localcontext
from fractions import Fraction
from functools import reduce
from operator import mul
from statistics import median
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

Number = Annotated[Decimal, Field(ge=-10**15, le=10**15, max_digits=28, decimal_places=12)]


class CalculatorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    operation: Literal["sum", "subtract", "product", "divide", "percentage",
                       "percent_of", "percent_change", "mean", "median", "min", "max"] | None = None
    values: list[Number] | None = Field(default=None, min_length=1, max_length=100)
    expression: str | None = Field(default=None, min_length=1, max_length=2000, strict=True)
    equations: dict[str, str] | None = Field(default=None, min_length=1, max_length=50)
    variables: dict[str, JsonValue] = Field(default_factory=dict, max_length=100)
    decimal_places: int | None = Field(default=None, ge=0, le=12, strict=True)


def calculate(payload: dict) -> dict:
    """One calculator: provide expression OR equations, with optional variables.

    Supports + - * / // % ** (integer exponent -100..100), parentheses,
    comparisons, and/or/not, and x if condition else y. Functions: sum,
    product, mean, median, min, max, count, abs, sqrt, ceil, floor, round.
    Aggregates accept [numbers] or multiple numbers. round(x, places) is
    half-up (0..12 places). days_between(start, end) uses ISO dates;
    hours_between(start, end) requires ISO timestamps with UTC offsets.
    Dates must be ordered. Use decimal_places for final output rounding only.
    Arithmetic has 50 significant digits; magnitude is bounded to 1e-30..1e30
    or zero. Missing values, unsafe syntax and failed batches return errors.
    Legacy operation/values calls remain supported. No currency conversion
    or price discovery is automatic. All tools use this same callable.
    """
    try:
        request = CalculatorInput.model_validate(payload)
        if sum(x is not None for x in (request.operation, request.expression, request.equations)) != 1:
            raise ValueError("Supply exactly one of expression, equations or operation")
        if request.operation is None and request.values is not None:
            raise ValueError("values is only used with legacy operation")
        with localcontext() as context:
            context.prec = 50
            def number(value):
                if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)) or len(str(value)) > 120:
                    raise ValueError("A finite number is required; unknown values are not zero")
                value = Decimal(str(value))
                if not value.is_finite() or abs(value) > Decimal("1e30") or value and abs(value) < Decimal("1e-30"):
                    raise ValueError("Number exceeds calculator limits")
                return value

            def rounded(value, places=0):
                places = number(places)
                if places != int(places) or not 0 <= places <= 12:
                    raise ValueError("Rounding places must be an integer from 0 to 12")
                return number(value).quantize(Decimal(1).scaleb(-int(places)), rounding=ROUND_HALF_UP)

            def interval(start, end, hours=False):
                if not isinstance(start, str) or not isinstance(end, str):
                    raise TypeError("Date/time inputs must be ISO strings")
                if hours:
                    start, end = datetime.fromisoformat(start), datetime.fromisoformat(end)
                    if start.utcoffset() is None or end.utcoffset() is None:
                        raise ValueError("Timestamps require UTC offsets")
                    delta = end.astimezone(UTC) - start.astimezone(UTC)
                    value = Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / 1000000
                    value /= 3600
                else:
                    value = Decimal((date.fromisoformat(end) - date.fromisoformat(start)).days)
                if value < 0:
                    raise ValueError("End must not precede start")
                return value

            functions = {"sum": lambda *v: sum(v, Decimal(0)), "product": lambda *v: reduce(mul, v, Decimal(1)),
                         "mean": lambda *v: sum(v, Decimal(0)) / len(v), "median": lambda *v: median(v),
                         "min": lambda *v: min(v), "max": lambda *v: max(v), "count": lambda *v: Decimal(len(v)),
                         "abs": abs, "sqrt": lambda v: v.sqrt(), "ceil": lambda v: Decimal(v.__ceil__()),
                         "floor": lambda v: Decimal(v.__floor__()), "round": rounded,
                         "days_between": interval, "hours_between": lambda a, b: interval(a, b, True)}
            if request.operation is not None:
                op, values = request.operation, request.values
                if not values or request.variables:
                    raise ValueError("Legacy operation needs values and no variables")
                if op == "subtract" and len(values) < 2:
                    raise ValueError("subtract requires at least two values")
                if op in {"divide", "percentage", "percent_of", "percent_change"} and len(values) != 2:
                    raise ValueError(f"{op} requires exactly two values")
                legacy = {"subtract": lambda: values[0] - sum(values[1:], Decimal(0)),
                          "divide": lambda: values[0] / values[1], "percentage": lambda: values[0] * values[1] / 100,
                          "percent_of": lambda: values[0] / values[1] * 100,
                          "percent_change": lambda: (values[1] - values[0]) / values[0] * 100}
                value = legacy[op]() if op in legacy else functions[op](*values)
                results = {"result": number(value)}
            else:
                equations = request.equations if request.equations is not None else {"_output": request.expression}
                def valid_name(name):
                    return name.isascii() and name.isidentifier() and not name.startswith("_") and not keyword.iskeyword(name) and name not in functions
                if any(not valid_name(n) for n in request.variables) or request.equations and any(not valid_name(n) for n in equations):
                    raise ValueError("Use ordinary variable names, without reserved function names")
                if set(equations) & set(request.variables):
                    raise ValueError("Equation names must not overwrite supplied variables")
                trees = {}
                for name, expression in equations.items():
                    if not 0 < len(expression) <= 2000:
                        raise ValueError("Expression length must be 1..2000")
                    tree = ast.parse(expression, mode="eval")
                    if sum(1 for _ in ast.walk(tree)) > 300:
                        raise ValueError("Expression is too large")
                    trees[name] = (tree.body, expression)
                results, active, steps = {}, set(), 0
                def remainder(a, b):
                    value = Fraction(a) % Fraction(b)
                    return Decimal(value.numerator) / Decimal(value.denominator)
                binary = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
                          ast.FloorDiv: lambda a, b: Decimal(Fraction(a) // Fraction(b)),
                          ast.Mod: remainder,
                          ast.Pow: operator.pow}
                compare = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Eq: operator.eq,
                           ast.NotEq: operator.ne, ast.Gt: operator.gt, ast.GtE: operator.ge}

                def resolve(name, depth):
                    if name in active:
                        raise ValueError("Circular equation dependency")
                    if name in results:
                        return results[name]
                    if name in request.variables:
                        value = request.variables[name]
                        if isinstance(value, str) and len(value) <= 120:
                            try:
                                return number(value)
                            except DecimalException:
                                return value
                        if isinstance(value, list) and 0 < len(value) <= 100:
                            return [number(v) for v in value]
                        return value if isinstance(value, bool) else number(value)
                    if name not in trees:
                        raise ValueError(f"Missing variable: {name}")
                    active.add(name)
                    node, source = trees[name]
                    results[name] = evaluate(node, source, depth + 1)
                    active.remove(name)
                    return results[name]

                def evaluate(node, source, depth=0):
                    nonlocal steps
                    steps += 1
                    if depth > 40 or steps > 10000:
                        raise ValueError("Calculation exceeds complexity limits")
                    def child(item):
                        return evaluate(item, source, depth + 1)
                    if isinstance(node, ast.Constant):
                        if isinstance(node.value, bool):
                            return node.value
                        if isinstance(node.value, str) and len(node.value) <= 120:
                            return node.value
                        return number(ast.get_source_segment(source, node))
                    if isinstance(node, ast.Name):
                        return resolve(node.id, depth)
                    if isinstance(node, (ast.List, ast.Tuple)) and 0 < len(node.elts) <= 100:
                        return [number(child(v)) for v in node.elts]
                    if isinstance(node, ast.BinOp) and type(node.op) in binary:
                        a, b = number(child(node.left)), number(child(node.right))
                        if isinstance(node.op, ast.Pow) and (b != int(b) or abs(b) > 100):
                            raise ValueError("Power exponent must be an integer from -100 to 100")
                        return number(binary[type(node.op)](a, b))
                    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Not)):
                        value = child(node.operand)
                        if isinstance(node.op, ast.Not):
                            return not truth(value)
                        return number(value) if isinstance(node.op, ast.UAdd) else -number(value)
                    if isinstance(node, ast.Compare) and all(type(op) in compare for op in node.ops):
                        a = number(child(node.left))
                        for op, right in zip(node.ops, node.comparators, strict=True):
                            b = number(child(right))
                            if not compare[type(op)](a, b):
                                return False
                            a = b
                        return True
                    if isinstance(node, ast.BoolOp):
                        for item in node.values:
                            value = truth(child(item))
                            if isinstance(node.op, ast.And) and not value or isinstance(node.op, ast.Or) and value:
                                return value
                        return value
                    if isinstance(node, ast.IfExp):
                        return child(node.body if truth(child(node.test)) else node.orelse)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in functions and not node.keywords:
                        args = [child(a) for a in node.args]
                        if node.func.id not in {"days_between", "hours_between"}:
                            args = args[0] if len(args) == 1 and isinstance(args[0], list) else args
                            if not args:
                                raise ValueError("Functions require nonempty arguments")
                            args = [number(a) for a in args]
                        return number(functions[node.func.id](*args))
                    raise ValueError("Unsupported expression; only calculator operations are allowed")

                def truth(value):
                    if not isinstance(value, bool):
                        raise TypeError("Conditions must be comparisons or booleans")
                    return value

                for name in equations:
                    resolve(name, 0)
            output = {}
            for name, value in results.items():
                if isinstance(value, bool):
                    output[name] = value
                    continue
                value = number(value)
                if request.decimal_places is not None:
                    value = rounded(value, request.decimal_places)
                text = format(value, "f")
                output[name] = text.rstrip("0").rstrip(".") if request.decimal_places is None and "." in text else text
            return {"status": "ok", "results": {n: output[n] for n in equations}} if request.equations is not None else {"status": "ok", "result": output["result" if request.operation is not None else "_output"]}
    except ValidationError as exc:
        return {"status": "error", "error": exc.errors(include_url=False)[0]["msg"]}
    except (DecimalException, ZeroDivisionError):
        return {"status": "error", "error": "Invalid arithmetic (for example, division by zero)"}
    except (ValueError, SyntaxError, TypeError, RecursionError) as exc:
        return {"status": "error", "error": str(exc) if isinstance(exc, ValueError) else "Invalid expression or function arguments"}


CALCULATOR_TOOL = {
    "name": "calculator",
    "description": calculate.__doc__,
    "parameters": CalculatorInput.model_json_schema(),
    "function": calculate,
}
