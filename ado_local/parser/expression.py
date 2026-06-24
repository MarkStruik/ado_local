from __future__ import annotations

import re
from typing import Any

COUNTER_RE = re.compile(r"\$\[counter\((?P<prefix>.+?),\s*(?P<seed>\d+)\)\]")
RUNTIME_EXPR_RE = re.compile(r"\$\[([^\]]+)\]")
TEMPLATE_EXPR_RE = re.compile(r"\$\{\{([^}]+)\}\}")


def eval_runtime_expression(expr: str, context: dict[str, Any], counters: dict[str, int] | None = None) -> str:
    if counters is None:
        counters = {}

    def eval_counter_prefix(value: str) -> str:
        value = value.strip()
        m = re.fullmatch(r"variables\[['\"]([^'\"]+)['\"]\]", value)
        if m:
            return str(context.get("variables", {}).get(m.group(1), m.group(1)))
        if value.startswith("variables."):
            name = value[len("variables."):]
            return str(context.get("variables", {}).get(name, name))
        if (value.startswith("'") and value.endswith("'")) or (value.startswith('"') and value.endswith('"')):
            return value[1:-1]
        return value

    def replace_counter(m: re.Match) -> str:
        name = eval_counter_prefix(m.group("prefix"))
        seed = int(m.group("seed"))
        key = f"counter:{name}"
        current = counters.get(key, seed - 1)
        next_val = current + 1
        counters[key] = next_val
        return str(next_val)

    result = COUNTER_RE.sub(replace_counter, expr)

    def replace_format(m: re.Match) -> str:
        inner = m.group(1).strip()
        if inner.startswith("format("):
            pass
        return m.group(0)

    result = RUNTIME_EXPR_RE.sub(replace_format, result)
    return result


def eval_template_expression(expr: str, context: dict[str, Any]) -> Any:
    stripped = expr.strip()
    if stripped.startswith("parameters."):
        name = stripped[len("parameters."):]
        return context.get("parameters", {}).get(name)
    if stripped.startswith("variables."):
        name = stripped[len("variables."):]
        return context.get("variables", {}).get(name)
    if stripped == "true":
        return True
    if stripped == "false":
        return False
    return stripped


def expand_template_expressions(data: Any, context: dict[str, Any]) -> Any:
    if isinstance(data, str):
        def replace(m: re.Match) -> str:
            val = eval_template_expression(m.group(1), context)
            return str(val) if val is not None else m.group(0)
        return TEMPLATE_EXPR_RE.sub(replace, data)
    elif isinstance(data, dict):
        return {k: expand_template_expressions(v, context) for k, v in data.items()}
    elif isinstance(data, list):
        return [expand_template_expressions(item, context) for item in data]
    return data
