from __future__ import annotations

import json
import shutil
import sys
import urllib.request
import urllib.error
import zipfile
from pathlib import Path
from typing import Any, Optional

AGENT_CDN = "https://vstsagentpackage.azureedge.net/agent"
AGENT_GH = "https://github.com/microsoft/azure-pipelines-agent/releases/download/v{version}"
DEFAULT_AGENT_VERSION = "4.248.0"


def _platform_agent_file() -> str:
    if sys.platform.startswith("win"):
        return "vsts-agent-win-x64-{version}.zip"
    elif sys.platform == "darwin":
        return "vsts-agent-osx-x64-{version}.zip"
    return "vsts-agent-linux-x64-{version}.zip"


def _agent_urls(version: str) -> list[str]:
    filename = _platform_agent_file().format(version=version)
    return [
        f"{AGENT_CDN}/{version}/{filename}",
        f"{AGENT_GH.format(version=version)}/{filename}",
    ]


def _download_file(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "ado-local/0.1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        with open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)
    if dest.stat().st_size == 0:
        raise RuntimeError("Downloaded file is empty")


def _extract_task_version(data: bytes) -> str | None:
    try:
        v = json.loads(data).get("version", {})
        return f"{v.get('Major', 0)}.{v.get('Minor', 0)}.{v.get('Patch', 0)}"
    except Exception:
        return None


def _find_task_in_zip(zip_path: Path, task_name: str) -> dict[str, list[tuple[str, bytes]]] | None:
    versions: dict[str, list[tuple[str, bytes]]] = {}
    prefix_candidates = [f"_tasks/{task_name}/", f"_tasks/{task_name}V", f"_tasks/{task_name}_"]

    with zipfile.ZipFile(zip_path, "r") as zf:
        all_names = zf.namelist()
        task_dirs = set()
        for name in all_names:
            if name.startswith("_tasks/") and name.count("/") >= 2:
                parts = name.split("/")
                task_dir_name = parts[1]
                task_dirs.add(task_dir_name)

        matched_dir = None
        lower_task = task_name.lower()
        for d in sorted(task_dirs):
            if d.lower() == lower_task:
                matched_dir = d
                break
        if matched_dir is None:
            for d in sorted(task_dirs):
                if d.lower().startswith(lower_task) or lower_task.startswith(d.lower().replace("v", "")):
                    matched_dir = d
                    break

        if matched_dir is None:
            return None

        prefix = f"_tasks/{matched_dir}/"
        for name in all_names:
            if not name.startswith(prefix) or name.endswith("/"):
                continue
            rel = name[len(prefix):]
            parts = rel.split("/", 1)
            if len(parts) >= 2:
                ver, inner = parts[0], parts[1]
                data = zf.read(name)
                versions.setdefault(ver, []).append((inner, data))

    return versions if versions else None


def _download_agent(version: str, cache_dir: Path) -> Path | None:
    agent_cache = cache_dir.parent / "_agent_cache"
    agent_cache.mkdir(parents=True, exist_ok=True)
    zip_path = agent_cache / f"agent-{version}.zip"
    if zip_path.exists():
        return zip_path

    urls = _agent_urls(version)
    for url in urls:
        try:
            print(f"  Downloading agent v{version}...", file=sys.stderr)
            _download_file(url, zip_path)
            size = zip_path.stat().st_size
            print(f"  Downloaded ({size / 1024 / 1024:.1f} MB)", file=sys.stderr)
            return zip_path
        except Exception as e:
            print(f"  Failed: {e}", file=sys.stderr)
            if zip_path.exists():
                zip_path.unlink()
            continue
    return None


def download_task(
    task_spec: str,
    cache_dir: Path,
    agent_version: str = DEFAULT_AGENT_VERSION,
) -> Path | None:
    if "@" in task_spec:
        task_name, version_spec = task_spec.split("@", 1)
    else:
        task_name, version_spec = task_spec, ""

    task_dir = cache_dir / task_name
    if task_dir.exists():
        existing = sorted(task_dir.iterdir(), reverse=True) if task_dir.is_dir() else []
        if existing:
            return task_dir / existing[0].name

    print(f"  Resolving {task_spec}...", file=sys.stderr)

    agent_zip = _download_agent(agent_version, cache_dir)
    if agent_zip is None:
        print(f"  Could not download Azure Pipelines agent", file=sys.stderr)
        return None

    versions = _find_task_in_zip(agent_zip, task_name)
    if versions is None:
        print(f"  Task '{task_name}' not found in agent package", file=sys.stderr)
        return None

    resolved_version = None
    for ver in versions:
        task_json_data = None
        for inner, data in versions[ver]:
            if inner == "task.json":
                task_json_data = data
                break
        if task_json_data:
            r = _extract_task_version(task_json_data)
            if r:
                resolved_version = r
                break

    if not resolved_version:
        resolved_version = max(versions.keys())

    if version_spec:
        major = version_spec.split(".")[0]
        matching = [v for v in versions if v.startswith(major) or resolved_version.startswith(major)]
        if not matching:
            print(f"  Warning: requested v{version_spec}, found v{resolved_version}", file=sys.stderr)

    cached = task_dir / resolved_version
    if cached.exists():
        return cached

    files = versions.get(resolved_version)
    if not files:
        print(f"  No files for version {resolved_version}", file=sys.stderr)
        return None

    cached.mkdir(parents=True, exist_ok=True)
    for inner_path, data in files:
        dest = cached / inner_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    if not (cached / "task.json").exists():
        print(f"  Extracted task has no task.json", file=sys.stderr)
        shutil.rmtree(cached)
        return None

    print(f"  Cached: {task_name} {resolved_version}", file=sys.stderr)
    return cached


def download_all_tasks(task_specs: list[str], cache_dir: Path) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for spec in task_specs:
        path = download_task(spec, cache_dir)
        results[spec] = path is not None
    return results
