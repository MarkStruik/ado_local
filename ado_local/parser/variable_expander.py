from __future__ import annotations

import re
from typing import Any

VAR_REF_RE = re.compile(r"\$\(([\w.]+)\)")


def expand_variables(
    value: Any,
    variables: dict[str, Any],
    nested: bool = True,
    max_depth: int = 10,
) -> Any:
    if isinstance(value, str):
        result = value
        depth = 0
        while depth < max_depth:
            def replace(m: re.Match) -> str:
                name = m.group(1)
                val = variables.get(name)
                return str(val) if val is not None else m.group(0)
            new_result = VAR_REF_RE.sub(replace, result)
            if new_result == result:
                break
            result = new_result
            depth += 1
        return result
    elif isinstance(value, dict):
        return {k: expand_variables(v, variables, nested, max_depth) for k, v in value.items()}
    elif isinstance(value, list):
        return [expand_variables(item, variables, nested, max_depth) for item in value]
    return value


def resolve_nested_variable(name: str, variables: dict[str, Any]) -> Any:
    parts = name.split(".")
    current: Any = variables
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def collect_variable_refs(data: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(data, str):
        refs.update(VAR_REF_RE.findall(data))
    elif isinstance(data, dict):
        for v in data.values():
            refs.update(collect_variable_refs(v))
    elif isinstance(data, list):
        for item in data:
            refs.update(collect_variable_refs(item))
    return refs
