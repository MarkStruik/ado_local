from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from ado_local.models.pipeline import (
    CheckoutStep,
    Job,
    Pipeline,
    PublishStep,
    ScriptStep,
    Stage,
    Step,
    TaskStep,
)
from ado_local.parser.yaml_loader import load_pipeline_yaml


MAX_TEMPLATE_FILES = 100
MAX_TEMPLATE_DEPTH = 100

_TEMPLATE_EXPR_RE = re.compile(r"^\s*\$\{\{\s*(.*?)\s*\}\}\s*$")
_TEMPLATE_INTERP_RE = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
_IF_RE = re.compile(r"^if\s+(.+)$")
_EACH_RE = re.compile(r"^each\s+(\w+)\s+in\s+(.+)$")


class PipelineCompileError(ValueError):
    pass


def load_and_compile_pipeline(
    path: str | Path,
    parameters: dict[str, Any] | None = None,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pipeline_path = Path(path).resolve()
    data = load_pipeline_yaml(pipeline_path)
    root_dir = pipeline_path.parent
    params = _merge_parameters(data.get("parameters", []), parameters or {})
    vars_ = _collect_variables(data.get("variables", {}))
    vars_.update(variables or {})
    context = {"parameters": params, "variables": vars_}
    state = {"files": 1}
    compiled = _expand_templates(
        data,
        current_file=pipeline_path,
        root_dir=root_dir,
        context=context,
        depth=0,
        stack=[pipeline_path],
        state=state,
        insertion_key=None,
    )
    compiled = _expand_template_nodes(compiled, context)
    if isinstance(compiled, dict):
        compiled["parameters"] = params
        compiled["variables"] = vars_
    return compiled


def parse_pipeline_model(data: dict[str, Any], pipeline_path: str | Path) -> Pipeline:
    path = Path(pipeline_path)
    variables = _collect_variables(data.get("variables", {}))
    params = _collect_parameter_values(data.get("parameters", {}))
    pipeline = Pipeline(name=data.get("name", path.stem), variables=variables, parameters=params)

    if isinstance(data.get("stages"), list):
        pipeline.stages = [_parse_stage(item) for item in data["stages"] if isinstance(item, dict)]
    elif isinstance(data.get("jobs"), list):
        pipeline.jobs = [_parse_job(item) for item in data["jobs"] if isinstance(item, dict)]
    else:
        steps = [_parse_step(item) for item in data.get("steps", []) if isinstance(item, dict)]
        pipeline.jobs = [Job(name="default", steps=[s for s in steps if s is not None])]
    return pipeline


def collect_pipeline_tasks(data: Any) -> list[str]:
    specs: list[str] = []
    for step in collect_pipeline_steps(data):
        spec = step.get("task")
        if isinstance(spec, str) and spec:
            specs.append(spec)
    return list(dict.fromkeys(specs))


def collect_pipeline_steps(data: Any) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        return steps
    raw_steps = data.get("steps")
    if isinstance(raw_steps, list):
        steps.extend(item for item in raw_steps if isinstance(item, dict))
    raw_jobs = data.get("jobs")
    if isinstance(raw_jobs, list):
        for job in raw_jobs:
            if isinstance(job, dict):
                steps.extend(collect_pipeline_steps(job))
    raw_stages = data.get("stages")
    if isinstance(raw_stages, list):
        for stage in raw_stages:
            if isinstance(stage, dict):
                steps.extend(collect_pipeline_steps(stage))
    return steps


def evaluate_condition(
    condition: str | None,
    variables: dict[str, Any],
    succeeded: bool = True,
    failed: bool = False,
    canceled: bool = False,
) -> bool:
    expr = condition or "succeeded()"
    context = {
        "variables": variables,
        "succeeded": succeeded,
        "failed": failed,
        "canceled": canceled,
    }
    return _truthy(_eval_expr(expr, context))


def _parse_stage(data: dict[str, Any]) -> Stage:
    jobs = [_parse_job(item) for item in data.get("jobs", []) if isinstance(item, dict)]
    return Stage(
        name=str(data.get("stage") or data.get("name") or "stage"),
        display_name=data.get("displayName"),
        jobs=jobs,
        condition=data.get("condition"),
        variables=_collect_variables(data.get("variables", {})),
    )


def _parse_job(data: dict[str, Any]) -> Job:
    steps = [_parse_step(item) for item in data.get("steps", []) if isinstance(item, dict)]
    return Job(
        name=str(data.get("job") or data.get("name") or "job"),
        display_name=data.get("displayName"),
        pool=data.get("pool") if isinstance(data.get("pool"), str) else None,
        steps=[s for s in steps if s is not None],
        variables=_collect_variables(data.get("variables", {})),
        condition=data.get("condition"),
    )


def _parse_step(data: dict[str, Any]) -> Step | None:
    if "task" in data:
        return TaskStep(
            task=str(data["task"]),
            display_name=data.get("displayName", data["task"]),
            inputs=data.get("inputs", {}) if isinstance(data.get("inputs", {}), dict) else {},
            condition=data.get("condition"),
            continue_on_error=bool(data.get("continueOnError", False)),
            enabled=bool(data.get("enabled", True)),
            env=data.get("env", {}) if isinstance(data.get("env", {}), dict) else {},
        )
    if "script" in data:
        return ScriptStep(
            script=str(data["script"]),
            display_name=data.get("displayName", "script"),
            condition=data.get("condition"),
            continue_on_error=bool(data.get("continueOnError", False)),
            enabled=bool(data.get("enabled", True)),
            env=data.get("env", {}) if isinstance(data.get("env", {}), dict) else {},
            working_directory=data.get("workingDirectory"),
        )
    if "powershell" in data or "pwsh" in data:
        return ScriptStep(
            script=str(data.get("powershell") or data.get("pwsh") or ""),
            display_name=data.get("displayName", "powershell"),
            condition=data.get("condition"),
            continue_on_error=bool(data.get("continueOnError", False)),
            enabled=bool(data.get("enabled", True)),
            env=data.get("env", {}) if isinstance(data.get("env", {}), dict) else {},
            working_directory=data.get("workingDirectory"),
        )
    if "checkout" in data:
        return CheckoutStep(
            checkout=str(data.get("checkout", "self")),
            display_name=data.get("displayName", f"checkout: {data.get('checkout', 'self')}"),
            submodules=bool(data.get("submodules", False)),
            persist_credentials=bool(data.get("persistCredentials", False)),
            lfs=bool(data.get("lfs", False)),
            path=data.get("path"),
            condition=data.get("condition"),
            continue_on_error=bool(data.get("continueOnError", False)),
            enabled=bool(data.get("enabled", True)),
        )
    if "publish" in data:
        return PublishStep(
            publish=str(data.get("publish", "")),
            artifact=str(data.get("artifact", "drop")),
            display_name=data.get("displayName", f"publish: {data.get('publish', '')}"),
            condition=data.get("condition"),
            continue_on_error=bool(data.get("continueOnError", False)),
            enabled=bool(data.get("enabled", True)),
        )
    return None


def _expand_templates(
    data: Any,
    current_file: Path,
    root_dir: Path,
    context: dict[str, Any],
    depth: int,
    stack: list[Path],
    state: dict[str, int],
    insertion_key: str | None,
) -> Any:
    if depth > MAX_TEMPLATE_DEPTH:
        raise PipelineCompileError("Template nesting limit exceeded")
    if isinstance(data, list):
        result: list[Any] = []
        for item in data:
            expanded = _expand_templates(item, current_file, root_dir, context, depth, stack, state, insertion_key)
            if isinstance(expanded, _Splice):
                result.extend(expanded.items)
            else:
                result.append(expanded)
        return result
    if isinstance(data, dict):
        if _is_template_reference(data):
            return _load_template_reference(data, current_file, root_dir, context, depth, stack, state, insertion_key)
        return {
            key: _expand_templates(value, current_file, root_dir, context, depth, stack, state, key)
            for key, value in data.items()
        }
    return data


class _Splice:
    def __init__(self, items: list[Any]) -> None:
        self.items = items


def _is_template_reference(data: dict[str, Any]) -> bool:
    return "template" in data and isinstance(data.get("template"), str)


def _load_template_reference(
    ref: dict[str, Any],
    current_file: Path,
    root_dir: Path,
    context: dict[str, Any],
    depth: int,
    stack: list[Path],
    state: dict[str, int],
    insertion_key: str | None,
) -> _Splice:
    template_name = ref["template"]
    if "@" in template_name:
        raise PipelineCompileError(f"Remote templates are not supported yet: {template_name}")
    template_path = _resolve_template_path(template_name, current_file, root_dir)
    if not template_path.exists():
        raise FileNotFoundError(f"Template file not found: {template_path}")
    if template_path in stack:
        chain = " -> ".join(str(p) for p in [*stack, template_path])
        raise PipelineCompileError(f"Template include cycle detected: {chain}")
    state["files"] = state.get("files", 0) + 1
    if state["files"] > MAX_TEMPLATE_FILES:
        raise PipelineCompileError("Template file include limit exceeded")

    raw = load_pipeline_yaml(template_path)
    ref_params = ref.get("parameters", {}) if isinstance(ref.get("parameters", {}), dict) else {}
    ref_params = _expand_template_nodes(ref_params, context)
    params = _merge_parameters(raw.get("parameters", []), ref_params)
    variables = {**context.get("variables", {}), **_collect_variables(raw.get("variables", {}))}
    template_context = {"parameters": params, "variables": variables}
    raw = _expand_templates(raw, template_path, root_dir, template_context, depth + 1, [*stack, template_path], state, None)
    raw = _expand_template_nodes(raw, template_context)
    if not isinstance(raw, dict):
        return _Splice([])
    key = insertion_key if insertion_key in {"steps", "jobs", "stages", "variables"} else None
    content = raw.get(key) if key else None
    if content is None:
        for candidate in ("steps", "jobs", "stages", "variables"):
            if candidate in raw:
                content = raw[candidate]
                break
    if content is None:
        return _Splice([])
    if isinstance(content, list):
        return _Splice(content)
    return _Splice([content])


def _resolve_template_path(name: str, current_file: Path, root_dir: Path) -> Path:
    raw = name.replace("\\", "/")
    if raw.startswith("/"):
        return (root_dir / raw.lstrip("/")).resolve()
    return (current_file.parent / raw).resolve()


def _expand_template_nodes(data: Any, context: dict[str, Any]) -> Any:
    if isinstance(data, list):
        result: list[Any] = []
        for item in data:
            expanded = _expand_template_nodes(item, context)
            if isinstance(expanded, _Splice):
                result.extend(expanded.items)
            elif expanded is not None:
                result.append(expanded)
        return result
    if isinstance(data, dict):
        if len(data) == 1:
            key = next(iter(data))
            expr = _template_expression_key(key)
            if expr:
                value = next(iter(data.values()))
                if_match = _IF_RE.match(expr)
                if if_match:
                    if _truthy(_eval_expr(if_match.group(1), context)):
                        expanded = _expand_template_nodes(value, context)
                        if isinstance(expanded, list):
                            return _Splice(expanded)
                        return expanded
                    return _Splice([])
                each_match = _EACH_RE.match(expr)
                if each_match:
                    name = each_match.group(1)
                    collection = _eval_expr(each_match.group(2), context)
                    items: list[Any] = []
                    if isinstance(collection, dict):
                        iterable = [{"key": k, "value": v} for k, v in collection.items()]
                    else:
                        iterable = collection if isinstance(collection, list) else []
                    for item in iterable:
                        sub_context = copy.deepcopy(context)
                        sub_context[name] = item
                        expanded = _expand_template_nodes(copy.deepcopy(value), sub_context)
                        if isinstance(expanded, _Splice):
                            items.extend(expanded.items)
                        elif isinstance(expanded, list):
                            items.extend(expanded)
                        elif expanded is not None:
                            items.append(expanded)
                    return _Splice(items)
        result: dict[Any, Any] = {}
        for key, value in data.items():
            expr = _template_expression_key(key)
            if expr:
                if_match = _IF_RE.match(expr)
                if if_match and _truthy(_eval_expr(if_match.group(1), context)):
                    expanded = _expand_template_nodes(value, context)
                    if isinstance(expanded, dict):
                        result.update(expanded)
                continue
            result[_expand_template_nodes(key, context)] = _expand_template_nodes(value, context)
        return result
    if isinstance(data, str):
        whole = _TEMPLATE_EXPR_RE.match(data)
        if whole:
            return _eval_expr(whole.group(1), context)
        return _TEMPLATE_INTERP_RE.sub(lambda m: str(_eval_expr(m.group(1), context) or ""), data)
    return data


def _template_expression_key(key: Any) -> str | None:
    if not isinstance(key, str):
        return None
    m = _TEMPLATE_EXPR_RE.match(key)
    return m.group(1).strip() if m else None


def _merge_parameters(defs: Any, overrides: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if isinstance(defs, list):
        for item in defs:
            if isinstance(item, dict) and "name" in item:
                params[str(item["name"])] = item.get("default")
    elif isinstance(defs, dict):
        params.update(defs)
    params.update(overrides)
    return params


def _collect_parameter_values(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    return _merge_parameters(raw, {})


def _collect_variables(raw: Any) -> dict[str, Any]:
    variables: dict[str, Any] = {}
    if isinstance(raw, dict):
        variables.update(raw)
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and "name" in item:
                variables[str(item["name"])] = item.get("value", "")
            elif isinstance(item, dict) and "template" not in item and "group" not in item:
                variables.update(item)
    return variables


def _eval_expr(expr: str, context: dict[str, Any]) -> Any:
    expr = expr.strip()
    if expr.lower() == "true":
        return True
    if expr.lower() == "false":
        return False
    if expr.lower() == "null":
        return None
    if (expr.startswith("'") and expr.endswith("'")) or (expr.startswith('"') and expr.endswith('"')):
        return expr[1:-1].replace("''", "'")
    if re.fullmatch(r"-?\d+", expr):
        return int(expr)
    if re.fullmatch(r"-?\d+\.\d+", expr):
        return float(expr)
    call = _parse_call(expr)
    if call:
        name, args = call
        vals = [_eval_expr(arg, context) for arg in args]
        return _eval_function(name, vals, context)
    return _resolve_ref(expr, context)


def _parse_call(expr: str) -> tuple[str, list[str]] | None:
    m = re.match(r"^(\w+)\((.*)\)$", expr, re.DOTALL)
    if not m:
        return None
    return m.group(1), _split_args(m.group(2))


def _split_args(text: str) -> list[str]:
    args: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
        elif ch in {"'", '"'}:
            quote = ch
            current.append(ch)
        elif ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    if current or text.strip():
        args.append("".join(current).strip())
    return args


def _eval_function(name: str, args: list[Any], context: dict[str, Any]) -> Any:
    lname = name.lower()
    if lname == "succeeded":
        return bool(context.get("succeeded", True))
    if lname == "failed":
        return bool(context.get("failed", False))
    if lname == "canceled":
        return bool(context.get("canceled", False))
    if lname == "always":
        return True
    if lname == "succeededorfailed":
        return bool(context.get("succeeded", True) or context.get("failed", False))
    if lname == "eq":
        return len(args) == 2 and str(args[0]).lower() == str(args[1]).lower()
    if lname == "ne":
        return not _eval_function("eq", args, context)
    if lname == "and":
        return all(_truthy(arg) for arg in args)
    if lname == "or":
        return any(_truthy(arg) for arg in args)
    if lname == "not":
        return len(args) == 1 and not _truthy(args[0])
    if lname == "contains" and len(args) == 2:
        return str(args[1]).lower() in str(args[0]).lower()
    if lname == "startswith" and len(args) == 2:
        return str(args[0]).lower().startswith(str(args[1]).lower())
    if lname == "endswith" and len(args) == 2:
        return str(args[0]).lower().endswith(str(args[1]).lower())
    if lname == "in" and args:
        left = str(args[0]).lower()
        return any(left == str(arg).lower() for arg in args[1:])
    if lname == "coalesce":
        for arg in args:
            if arg not in (None, ""):
                return arg
        return None
    return None


def _resolve_ref(expr: str, context: dict[str, Any]) -> Any:
    m = re.match(r"^(\w+)\[['\"]([^'\"]+)['\"]\]$", expr)
    if m:
        root = context.get(m.group(1), {})
        return root.get(m.group(2)) if isinstance(root, dict) else None
    parts = expr.split(".")
    current: Any = context
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current if current is not None else expr


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value != "" and value.lower() != "false"
    return bool(value)
