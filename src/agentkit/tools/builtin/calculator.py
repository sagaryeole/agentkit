import ast
import math
import operator
from collections.abc import Callable
from typing import Any

from agentkit.tools.base import ToolFailure, tool

_BINARY: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_FUNCTIONS: dict[str, Callable[..., Any]] = {
    name: getattr(math, name)
    for name in ("sqrt", "exp", "log", "log10", "log2", "sin", "cos", "tan", "floor", "ceil")
} | {"abs": abs, "round": round, "min": min, "max": max}
_CONSTANTS = {"pi": math.pi, "e": math.e}
_MAX_RESULT_BITS = 100_000


def _unsupported(what: str) -> ToolFailure:
    return ToolFailure(
        "invalid_input",
        f"{what} is not supported. Use numbers, + - * / // % **, parentheses, pi, e and "
        f"the functions {', '.join(sorted(_FUNCTIONS))}.",
    )


def _evaluate(node: ast.AST) -> Any:
    """Walk the parsed expression, allowing only arithmetic. Nothing is ever `eval`ed."""
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, int | float) and not isinstance(node.value, bool):
            return node.value
        raise _unsupported(f"The value {node.value!r}")
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left, right = _evaluate(node.left), _evaluate(node.right)
        huge_power = (
            isinstance(node.op, ast.Pow)
            and isinstance(left, int)
            and isinstance(right, int)
            and left.bit_length() * max(right, 1) > _MAX_RESULT_BITS
        )
        if huge_power:
            raise ToolFailure("invalid_input", "That power is too large to compute.")
        return _BINARY[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_evaluate(node.operand))
    if isinstance(node, ast.Name) and node.id in _CONSTANTS:
        return _CONSTANTS[node.id]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _FUNCTIONS
        and not node.keywords
    ):
        return _FUNCTIONS[node.func.id](*(_evaluate(arg) for arg in node.args))
    raise _unsupported(f"'{ast.unparse(node)}'")


@tool(side_effects="none", idempotent=True, tags=["math"])
def calculator(expression: str) -> int | float:
    """Evaluate an arithmetic expression and return the exact numeric result.

    Use this for any calculation instead of working it out yourself.
    Do not use it for symbolic algebra, units or dates.

    Args:
        expression: Arithmetic such as "(1717 * 2323) / 7" or "sqrt(2) * pi". Supports
            + - * / // % **, parentheses, pi, e, and sqrt, exp, log, log10, log2, sin, cos,
            tan, floor, ceil, abs, round, min, max.
    """
    if len(expression) > 500:
        raise ToolFailure("invalid_input", "The expression is longer than 500 characters.")
    try:
        result = _evaluate(ast.parse(expression.strip(), mode="eval"))
    except SyntaxError as exc:
        raise ToolFailure(
            "invalid_input", f"'{expression}' is not a valid arithmetic expression."
        ) from exc
    except (ArithmeticError, ValueError, TypeError) as exc:
        raise ToolFailure("invalid_input", f"Cannot compute '{expression}': {exc}.") from exc
    if not isinstance(result, int | float):
        raise ToolFailure("invalid_input", f"'{expression}' has no real-number result.")
    return result
