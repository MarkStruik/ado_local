from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ado_local.models.config import LocalSettings
from ado_local.models.pipeline import Pipeline
from ado_local.parser.yaml_loader import load_pipeline_yaml
from ado_local.parser.variable_expander import collect_variable_refs
from ado_local.cache.task_cache import resolve_task, list_tasks


class AnalysisResult:
    def __init__(self) -> None:
        self.missing_variables: list[str] = []
        self.missing_parameters: list[str] = []
        self.missing_tasks: list[str] = []
        self.missing_service_connections: list[str] = []
        self.warnings: list[str] = []
        self.task_count: int = 0
        self.variable_count: int = 0

    @property
    def has_issues(self) -> bool:
        return bool(
            self.missing_variables
            or self.missing_parameters
            or self.missing_tasks
            or self.missing_service_connections
        )

    def summary(self) -> str:
        parts = []
        if self.missing_variables:
            parts.append(f"Missing variables: {len(self.missing_variables)}")
        if self.missing_parameters:
            parts.append(f"Missing parameters: {len(self.missing_parameters)}")
        if self.missing_tasks:
            parts.append(f"Missing tasks: {len(self.missing_tasks)}")
        if self.missing_service_connections:
            parts.append(f"Missing service connections: {len(self.missing_service_connections)}")
        if not parts:
            return "No issues found"
        return ", ".join(parts)


def analyze_pipeline(
    pipeline_yaml: dict[str, Any],
    settings: LocalSettings,
    cache_dir: Path,
) -> AnalysisResult:
    result = AnalysisResult()

    defined_vars = _collect_defined_vars(pipeline_yaml, settings)
    defined_params = _collect_defined_params(pipeline_yaml, settings)

    all_refs = collect_variable_refs(pipeline_yaml)
    for ref in all_refs:
        if ref not in defined_vars and ref not in defined_params:
            if ref not in _PREDEFINED:
                result.missing_variables.append(ref)

    params_raw = pipeline_yaml.get("parameters", [])
    if isinstance(params_raw, list):
        for p in params_raw:
            if isinstance(p, dict) and p.get("required") and p["name"] not in settings.parameters:
                result.missing_parameters.append(p["name"])
    elif isinstance(params_raw, dict):
        pass

    steps = _collect_steps(pipeline_yaml)
    result.task_count = len(steps)
    for step in steps:
        task_spec = step.get("task", "")
        if task_spec:
            resolved = resolve_task(task_spec, cache_dir, auto_download=False)
            if resolved is None:
                result.missing_tasks.append(task_spec)

    result.missing_service_connections = _check_service_connections(pipeline_yaml)

    return result


def _collect_steps(pipeline_yaml: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for stage in pipeline_yaml.get("stages", []):
        for job in stage.get("jobs", []):
            steps.extend(job.get("steps", []))
    for job in pipeline_yaml.get("jobs", []):
        steps.extend(job.get("steps", []))
    steps.extend(pipeline_yaml.get("steps", []))
    return steps


def _check_service_connections(pipeline_yaml: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for step in _collect_steps(pipeline_yaml):
        inputs = step.get("inputs", {})
        for value in inputs.values():
            if isinstance(value, str) and value.startswith("isrsc-"):
                missing.append(value)
    return list(set(missing))


def _collect_defined_vars(pipeline_yaml: dict[str, Any], settings: LocalSettings) -> set[str]:
    vars_set: set[str] = set()
    raw = pipeline_yaml.get("variables", {})
    if isinstance(raw, dict):
        vars_set.update(raw.keys())
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                if "name" in item:
                    vars_set.add(item["name"])
                elif "group" in item:
                    pass
    vars_set.update(settings.variables.keys())
    return vars_set


def _collect_defined_params(pipeline_yaml: dict[str, Any], settings: LocalSettings) -> set[str]:
    params_set: set[str] = set()
    raw = pipeline_yaml.get("parameters", [])
    if isinstance(raw, list):
        for p in raw:
            if isinstance(p, dict) and "name" in p:
                params_set.add(p["name"])
    elif isinstance(raw, dict):
        params_set.update(raw.keys())
    params_set.update(settings.parameters.keys())
    return params_set


_PREDEFINED = {
    "Build.SourcesDirectory", "Build.StagingDirectory", "Build.BinariesDirectory",
    "Build.ArtifactStagingDirectory", "Agent.TempDirectory", "Agent.ToolsDirectory",
    "Agent.WorkFolder", "Agent.HomeDirectory", "Agent.Id", "Agent.Name",
    "Agent.MachineName", "Agent.Version", "System.DefaultWorkingDirectory",
    "System.TeamProject", "System.TeamFoundationCollectionUri",
    "System.ArtifactsDirectory", "Build.BuildId", "Build.BuildNumber",
    "Build.DefinitionName", "Build.Repository.LocalPath", "Build.Repository.Name",
    "Build.Repository.Provider", "System",
}
