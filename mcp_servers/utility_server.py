import ast
from datetime import datetime, timedelta, timezone
import operator
# Top of file:
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("kurukshetra-utility")

# Allowed safe math operators (no eval() security risk!)
_BINARY_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _safe_eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _safe_eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPS:
        left = _safe_eval_node(node.left)
        right = _safe_eval_node(node.right)
        # Guard against huge exponents freezing CPU
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ValueError("Exponent too large.")
        return float(_BINARY_OPS[type(node.op)](left, right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return float(_UNARY_OPS[type(node.op)](_safe_eval_node(node.operand)))
    raise ValueError(f"Unsupported math operation: {type(node).__name__}")


@mcp.tool()
def get_current_datetime() -> str:
    """
    Returns the exact current real-world date and time (both IST and UTC).
    Use this whenever time, current year, or recency matters in a debate.
    """
    utc_now = datetime.now(timezone.utc)
    ist_now = utc_now.astimezone(timezone(timedelta(hours=5, minutes=30)))
    return (
        f"Current Date/Time: {ist_now.strftime('%A, %B %d, %Y | %H:%M IST')} "
        f"({utc_now.strftime('%Y-%m-%d %H:%M UTC')})"
    )


@mcp.tool()
def calculate(expression: str) -> str:
    """
    Safely evaluates an arithmetic math expression for budgets, ROI, burn rates,
    or unit comparisons (e.g., '(200000 - 10000) / 12' or '180000 * 1.18').
    """
    cleaned = expression.strip().replace(",", "")
    if not cleaned:
        return "Error: expression cannot be empty."
    try:
        tree = ast.parse(cleaned, mode="eval")
        result = _safe_eval_node(tree)
        # Format cleanly (show integer if whole number, otherwise up to 4 decimals)
        formatted = f"{int(result):,}" if result.is_integer() else f"{result:,.4f}".rstrip("0").rstrip(".")
        return f"{expression} = {formatted}"
    except Exception as e:
        return f"Calculation error for '{expression}': {e}"


if __name__ == "__main__":
    mcp.run()