from __future__ import annotations

import re
from typing import Any

IF_RE = re.compile(
    r"\$\{\{\s*if\s+(.+?)\s*\}\}(.*?)(?:\$\{\{\s*else\s*\}\}(.*?))?\$\{\{\s*endif\s*\}\}",
    re.DOTALL,
)
EACH_RE = re.compile(
    r"\$\{\{\s*each\s+(\w+)\s+in\s+(\w+)\s*\}\}(.*?)\$\{\{\s*endeach\s*\}\}",
    re.DOTALL,
)


def process_conditionals(data: Any, context: dict[str, Any]) -> Any:
    if isinstance(data, str):
        def replace_if(m: re.Match) -> str:
            condition = m.group(1).strip()
            then_block = m.group(2) or ""
            else_block = m.group(3) or ""
            result = _eval_condition(condition, context)
            return then_block if result else else_block

        def replace_each(m: re.Match) -> str:
            var_name = m.group(1)
            collection_name = m.group(2)
            body = m.group(3)
            collection = context.get(collection_name, [])
            parts = []
            for item in collection:
                sub_context = {**context, var_name: item}
                parts.append(process_conditionals(body, sub_context))
            return "".join(parts)

        result = IF_RE.sub(replace_if, data)
        result = EACH_RE.sub(replace_each, result)
        return result

    elif isinstance(data, dict):
        return {k: process_conditionals(v, context) for k, v in data.items()}
    elif isinstance(data, list):
        return [process_conditionals(item, context) for item in data]
    return data


def _eval_condition(condition: str, context: dict[str, Any]) -> bool:
    parts = condition.split()
    if len(parts) == 1:
        val = _resolve_value(parts[0], context)
        return bool(val)
    if len(parts) == 3:
        left = _resolve_value(parts[0], context)
        op = parts[1]
        right = _resolve_value(parts[2], context)
        if op == "==":
            return left == right
        elif op == "!=":
            return left != right
        elif op == ">":
            return float(left) > float(right) if _is_numeric(left) and _is_numeric(right) else False
        elif op == "<":
            return float(left) < float(right) if _is_numeric(left) and _is_numeric(right) else False
        elif op == ">=":
            return float(left) >= float(right) if _is_numeric(left) and _is_numeric(right) else False
        elif op == "<=":
            return float(left) <= float(right) if _is_numeric(left) and _is_numeric(right) else False
    if "startsWith" in condition:
        m = re.search(r"startsWith\((\w+),\s*'([^']+)'\)", condition)
        if m:
            val = str(_resolve_value(m.group(1), context))
            return val.startswith(m.group(2))
    if "endsWith" in condition:
        m = re.search(r"endsWith\((\w+),\s*'([^']+)'\)", condition)
        if m:
            val = str(_resolve_value(m.group(1), context))
            return val.endswith(m.group(2))
    return False


def _resolve_value(token: str, context: dict[str, Any]) -> Any:
    if token.startswith("'"):
        return token.strip("'")
    if token == "true":
        return True
    if token == "false":
        return False
    parts = token.split(".")
    if len(parts) >= 2:
        root = context.get(parts[0], {})
        if isinstance(root, dict):
            return root.get(parts[1])
    return context.get(token, token)


def _is_numeric(v: Any) -> bool:
    try:
        float(v)
        return True
    except (ValueError, TypeError):
        return False
