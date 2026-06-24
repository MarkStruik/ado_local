from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

from ado_local.models.task import ResolvedTask, TaskDefinition


VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def resolve_task(
    task_spec: str,
    cache_dir: Path,
    auto_download: bool = True,
    log_callback: Callable[[str], None] | None = None,
    azure_devops_token: str | None = None,
    azure_devops_org: str | None = None,
    azure_devops_project: str | None = None,
) -> Optional[ResolvedTask]:
    if "@" in task_spec:
        name, version_spec = task_spec.split("@", 1)
    else:
        name = task_spec
        version_spec = ""

    task_dir = cache_dir / name
    if not task_dir.exists():
        if auto_download:
            downloaded = _try_download_task(task_spec, cache_dir, log_callback,
                                            token=azure_devops_token, org=azure_devops_org)
            if downloaded:
                task_dir = cache_dir / name
        if not task_dir.exists() and azure_devops_token and azure_devops_org:
            downloaded = _try_download_ado_task(task_spec, cache_dir, azure_devops_token, azure_devops_org, azure_devops_project, log_callback)
            if downloaded:
                task_dir = cache_dir / name
        if not task_dir.exists():
            return None

    version = _resolve_version(task_dir, version_spec)
    if not version:
        if auto_download:
            downloaded = _try_download_task(task_spec, cache_dir, log_callback,
                                            token=azure_devops_token, org=azure_devops_org)
            if downloaded:
                version = _resolve_version(task_dir, version_spec)
        if not version and azure_devops_token and azure_devops_org:
            downloaded = _try_download_ado_task(task_spec, cache_dir, azure_devops_token, azure_devops_org, azure_devops_project, log_callback)
            if downloaded:
                version = _resolve_version(task_dir, version_spec)
        if not version:
            return None

    task_path = task_dir / version
    task_json_path = task_path / "task.json"
    if not task_json_path.exists():
        return None

    definition = _parse_task_json(task_json_path)
    if definition is None:
        return None

    return ResolvedTask(
        name=name,
        version_spec=version_spec,
        resolved_version=version,
        path=str(task_path),
        definition=definition,
    )


def _try_download_task(task_spec: str, cache_dir: Path, log_callback: Callable[[str], None] | None = None,
                       token: str | None = None, org: str | None = None) -> bool:
    try:
        from ado_local.prepare.downloader import download_task
        result = download_task(task_spec, cache_dir, log_callback=log_callback,
                               azure_devops_token=token, azure_devops_org=org)
        return result is not None
    except Exception as e:
        import sys
        msg = f"  Failed to download {task_spec}: {e}"
        if log_callback:
            log_callback(msg)
        else:
            print(msg, file=sys.stderr)
        return False


def _try_download_ado_task(task_spec: str, cache_dir: Path, token: str, org: str, project: str | None = None, log_callback: Callable[[str], None] | None = None) -> bool:
    try:
        from ado_local.prepare.downloader import download_task_from_azure_devops
        result = download_task_from_azure_devops(task_spec, cache_dir, token, org, project, log_callback=log_callback)
        return result is not None
    except Exception as e:
        import sys
        msg = f"  Failed to download {task_spec} from Azure DevOps: {e}"
        if log_callback:
            log_callback(msg)
        else:
            print(msg, file=sys.stderr)
        return False


def _resolve_version(task_dir: Path, version_spec: str) -> Optional[str]:
    if not task_dir.exists():
        return None
    versions = sorted(
        (d.name for d in task_dir.iterdir() if d.is_dir()),
        key=_version_key,
        reverse=True,
    )
    if not versions:
        return None
    if not version_spec:
        return versions[0]
    major_spec = version_spec.split(".")[0]
    for v in versions:
        if v.startswith(major_spec):
            return v
    return versions[0]


def _version_key(v: str) -> tuple:
    m = VERSION_RE.match(v)
    if m:
        return tuple(int(x) for x in m.groups())
    return (0, 0, 0)


def _parse_task_json(path: Path) -> Optional[TaskDefinition]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    execution = data.get("execution", {})
    inputs = data.get("inputs", [])

    return TaskDefinition(
        name=data.get("name", ""),
        friendly_name=data.get("friendlyName"),
        description=data.get("description"),
        author=data.get("author"),
        help_url=data.get("helpUrl"),
        version=data.get("version", {}).get("Major", "") if isinstance(data.get("version"), dict) else str(data.get("version", "")),
        inputs=inputs,
        execution=execution,
        source_location=str(path.parent),
    )


def list_tasks(cache_dir: Path) -> list[str]:
    if not cache_dir.exists():
        return []
    return [d.name for d in cache_dir.iterdir() if d.is_dir()]
