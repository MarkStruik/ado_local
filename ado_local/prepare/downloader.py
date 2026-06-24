from __future__ import annotations

import json
import shutil
import sys
import urllib.request
import urllib.error
import zipfile
import base64
import io
from pathlib import Path
from typing import Any, Callable, Optional

AGENT_CDN = "https://vstsagentpackage.azureedge.net/agent"
AGENT_GH = "https://github.com/microsoft/azure-pipelines-agent/releases/download/v{version}"
AGENT_DEV = "https://download.agent.dev.azure.com/agent/{version}"
TASKS_REPO = "https://raw.githubusercontent.com/microsoft/azure-pipelines-tasks/master/Tasks"
DEFAULT_AGENT_VERSION = "4.275.0"
TASK_LIST_CACHE_FILE = "_task_list_cache.json"

_GH_TOKEN: str = ""


def _set_github_token(token: str) -> None:
    global _GH_TOKEN
    _GH_TOKEN = token


def _gh_headers() -> dict[str, str]:
    h = {"User-Agent": "ado-local/0.1.0"}
    if _GH_TOKEN:
        h["Authorization"] = f"Bearer {_GH_TOKEN}"
        h["Accept"] = "application/vnd.github.v3+json"
    return h


def _platform_agent_file() -> str:
    if sys.platform.startswith("win"):
        return "vsts-agent-win-x64-{version}.zip"
    elif sys.platform == "darwin":
        return "vsts-agent-osx-x64-{version}.zip"
    return "vsts-agent-linux-x64-{version}.zip"


def _agent_urls(version: str) -> list[str]:
    filename = _platform_agent_file().format(version=version)
    platform_alt = filename.replace("vsts-agent-", "pipelines-agent-")
    return [
        f"{AGENT_CDN}/{version}/{filename}",
        f"{AGENT_DEV.format(version=version)}/{filename}",
        f"{AGENT_GH.format(version=version)}/{filename}",
        f"{AGENT_GH.format(version=version)}/{platform_alt}",
    ]


def _has_common_substring(a: str, b: str, min_len: int = 4) -> bool:
    """Check if two strings share any common substring of min_len+ characters."""
    a = a.lower().replace("-", "").replace(".", "").replace(" ", "")
    b = b.lower().replace("-", "").replace(".", "").replace(" ", "")
    subs = set()
    for i in range(len(a) - min_len + 1):
        subs.add(a[i:i + min_len])
    for i in range(len(b) - min_len + 1):
        if b[i:i + min_len] in subs:
            return True
    return False


def _download_file(url: str, dest: Path, headers: dict | None = None) -> None:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "ado-local/0.1.0"})
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


def _task_list_cache_path(cache_dir: Path) -> Path:
    return cache_dir.parent / TASK_LIST_CACHE_FILE


def _fetch_task_list(token: str, org: str, cache_dir: Path) -> dict[str, dict[str, Any]]:
    """Fetch task name -> {guid, version} mapping from Azure DevOps API, with local caching."""
    cache_path = _task_list_cache_path(cache_dir)
    if cache_path.exists():
        age = (Path().stat() if False else None)  # placeholder
        try:
            data = json.loads(cache_path.read_text())
            if data:
                return data
        except Exception:
            pass

    url = f"https://dev.azure.com/{org}/_apis/distributedtask/tasks?api-version=7.1-preview.1&$top=500"
    headers = _adu_headers(token)
    req = urllib.request.Request(url, headers=headers)
    resp = urllib.request.urlopen(req, timeout=60)
    raw = resp.read()
    tasks_data = json.loads(raw)

    result: dict[str, dict[str, Any]] = {}
    for task in tasks_data.get("value", []):
        name = task.get("name", "")
        if not name:
            continue
        v = task.get("version", {})
        major, minor, patch = v.get('major', 0), v.get('minor', 0), v.get('patch', 0)
        version_str = f"{major}.{minor}.{patch}"
        key = name.lower()
        entry = result.setdefault(key, {"guid": task.get("id", ""), "versions": []})
        entry["versions"].append(version_str)
        # Keep the overall highest version as the default
        prev_best = entry.get("version")
        if not prev_best or (major, minor, patch) > tuple(int(x) for x in prev_best.split(".")):
            entry["version"] = version_str

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(result, indent=2))
    return result


def _download_task_from_distributed_api(
    task_name: str,
    version_spec: str,
    task_dir: Path,
    token: str,
    org: str,
    cache_dir: Path,
    _log: Callable[[str], None],
) -> Path | None:
    """Download complete task package (ZIP) from Azure DevOps distributedtask/tasks API."""
    task_list = _fetch_task_list(token, org, cache_dir)
    entry = task_list.get(task_name.lower())
    if not entry:
        _log(f"  Task '{task_name}' not found in Azure DevOps task list")
        return None

    task_guid = entry["guid"]
    task_version = entry["version"]
    all_versions = entry.get("versions", [task_version])

    # If version_spec specifies a major version, try to find matching version
    if version_spec:
        req_major = version_spec.split(".")[0]
        matching = [v for v in all_versions if v.startswith(req_major)]
        if matching:
            # Use the highest matching version
            matching.sort(key=lambda s: tuple(int(x) for x in s.split(".")), reverse=True)
            task_version = matching[0]
        else:
            _log(f"  Warning: requested v{version_spec}, latest is v{task_version}")

    download_url = f"https://dev.azure.com/{org}/_apis/distributedtask/tasks/{task_guid}/{task_version}"
    _log(f"  Downloading {task_name} v{task_version} from Azure DevOps...")
    try:
        req = urllib.request.Request(download_url, headers=_adu_headers(token))
        resp = urllib.request.urlopen(req, timeout=120)
        zip_data = resp.read()
        _log(f"  Downloaded {len(zip_data) / 1024:.0f} KB")
    except Exception as e:
        _log(f"  Failed to download task ZIP: {e}")
        return None

    target = task_dir / task_version
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(name))

    _log(f"  Cached: {task_name} {task_version}")
    return target


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


def _download_agent(
    version: str,
    cache_dir: Path,
    log_callback: Callable[[str], None] | None = None,
) -> Path | None:
    def _log(msg: str) -> None:
        if log_callback:
            log_callback(msg)
        else:
            print(msg, file=sys.stderr)

    agent_cache = cache_dir.parent / "_agent_cache"
    agent_cache.mkdir(parents=True, exist_ok=True)
    zip_path = agent_cache / f"agent-{version}.zip"
    if zip_path.exists():
        _log(f"  Agent package already cached")
        return zip_path

    urls = _agent_urls(version)
    for url in urls:
        try:
            _log(f"  Downloading from {url}")
            _download_file(url, zip_path)
            size = zip_path.stat().st_size
            _log(f"  Downloaded ({size / 1024 / 1024:.1f} MB)")
            return zip_path
        except Exception as e:
            _log(f"  Failed: {e}")
            if zip_path.exists():
                zip_path.unlink()
            continue
    return None


def _agent_externals_dir(cache_dir: Path) -> Path:
    return cache_dir.parent / "_agent_externals"


def _extract_agent_externals(zip_path: Path, cache_dir: Path) -> Path:
    """Extract the agent's externals/ directory for task tool resolution."""
    target = _agent_externals_dir(cache_dir)
    if target.exists():
        return target
    import zipfile as _zipfile
    target.mkdir(parents=True, exist_ok=True)
    with _zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            if name.startswith("externals/") and not name.endswith("/"):
                rel = name[len("externals/"):]
                dest = target / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(name))
    return target


def _find_github_task_folder(task_name: str) -> str | None:
    """Search GitHub for the task folder matching task_name (case-insensitive)."""
    import json as _json
    task_lower = task_name.lower()
    for suffix in ["", "V0", "V1", "V2"]:
        candidate = task_name + suffix
        url = f"{TASKS_REPO}/{candidate}/task.json"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ado-local/0.1.0"})
            urllib.request.urlopen(req, timeout=10)
            return candidate
        except Exception:
            continue
    # Search GitHub API for folders matching the task name
    url = f"https://api.github.com/repos/microsoft/azure-pipelines-tasks/contents/Tasks"
    try:
        req = urllib.request.Request(url, headers=_gh_headers())
        resp = urllib.request.urlopen(req, timeout=15)
        items = _json.loads(resp.read())
        candidates = []
        for item in items:
            if item["type"] == "dir" and task_lower in item["name"].lower():
                candidates.append(item["name"])
        # Prefer exact prefix match, then longest common prefix
        exact = [c for c in candidates if c.lower().startswith(task_lower)]
        if exact:
            return sorted(exact)[0]
        if candidates:
            return sorted(candidates, key=lambda c: len(c))[0]
    except Exception:
        pass
    return None


def _resolve_missing_externals(task_dir: Path, agent_zip: Path | None = None) -> None:
    import re
    _refs: set[str] = set()
    _pat = re.compile(r"""path\.join\s*\(\s*__dirname\s*,\s*['"]([^'"]+)['"]\s*\)""")
    for f in task_dir.rglob("*"):
        if f.suffix not in (".js", ".ts") or "node_modules" in f.parts:
            continue
        try:
            src = f.read_text("utf-8", errors="replace")
        except Exception:
            continue
        _refs.update(_pat.findall(src))
    if not _refs:
        return

    # Build candidate locations
    search_roots: list[Path] = [task_dir / "node_modules"]
    if agent_zip:
        search_roots.append(_agent_externals_dir(task_dir.parent.parent))

    for name in sorted(_refs):
        target = task_dir / name
        if target.exists():
            continue

        # Generate name variants for fuzzy matching
        variants = {name}
        p = Path(name)
        stem = p.stem
        # strip trailing 'r'  (7zr.exe -> 7z.exe)
        if stem.endswith("r"):
            variants.add(stem[:-1] + p.suffix)
        # also try just the stem (any extension)
        if p.suffix:
            variants.add(stem + p.suffix)

        found = False
        for root in search_roots:
            if not root.is_dir():
                continue
            for vname in variants:
                for matched in root.rglob(vname):
                    if matched.is_file():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(matched), str(target))
                        found = True
                        break
                if found:
                    break
            if found:
                break

        # Last resort: match by stem in externals/**/bin/ or externals/**/externals/
        if not found and p.suffix:
            for root in search_roots:
                if not root.is_dir():
                    continue
                for candidate in root.rglob(f"*{p.suffix}"):
                    parts = candidate.relative_to(root).parts
                    if "externals" not in parts and "bin" not in parts:
                        continue
                    c_stem = candidate.stem
                    if c_stem == stem or c_stem == stem.rstrip("r"):
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(candidate), str(target))
                        found = True
                        break
                if found:
                    break


def _download_task_from_github(
    task_name: str,
    task_dir: Path,
    version_spec: str,
    _log: Callable[[str], None],
    agent_zip: Path | None = None,
) -> Path | None:
    import subprocess
    folder = _find_github_task_folder(task_name)
    if not folder:
        _log(f"  Task '{task_name}' not found on GitHub")
        return None
    resolved_version = version_spec or "1.0.0"
    task_json_url = f"{TASKS_REPO}/{folder}/task.json"
    try:
        req = urllib.request.Request(task_json_url, headers={"User-Agent": "ado-local/0.1.0"})
        resp = urllib.request.urlopen(req, timeout=15)
        task_json = json.loads(resp.read())
        v = task_json.get("version", {})
        resolved_version = f"{v.get('Major', 1)}.{v.get('Minor', 0)}.{v.get('Patch', 0)}"
        target = task_dir / resolved_version
        target.mkdir(parents=True, exist_ok=True)
        (target / "task.json").write_text(json.dumps(task_json, indent=2))
        _log(f"  Downloading {folder} files...")
        _download_github_task_files(folder, target, task_json)
        if (target / "package.json").exists():
            _log(f"  Installing npm dependencies...")
            try:
                subprocess.run(["npm", "install", "--no-optional", "--no-fund", "--no-audit"], cwd=str(target), capture_output=True, text=True, timeout=120, shell=True)
            except Exception as e:
                _log(f"  npm install warning: {e}")
        if (target / "tsconfig.json").exists():
            _log(f"  Compiling TypeScript...")
            try:
                subprocess.run(["npx", "-p", "typescript", "tsc"], cwd=str(target), capture_output=True, text=True, timeout=120, shell=True)
            except Exception as e:
                _log(f"  tsc warning: {e}")
        _resolve_missing_externals(target, agent_zip)
        return target
    except urllib.error.HTTPError:
        _log(f"  Task '{folder}' not found on GitHub")
        return None
    except Exception as e:
        _log(f"  Error downloading {folder}: {e}")
        return None


def _download_github_task_files(folder: str, target: Path, task_json: dict) -> None:
    import subprocess
    handler_files: list[str] = []
    execution = task_json.get("execution", {})
    if isinstance(execution, dict):
        for key in ("Node", "Node10", "Node16", "Node20", "Node20_1",
                     "PowerShell", "PowerShell2", "PowerShell3", "Process"):
            handler = execution.get(key)
            if isinstance(handler, dict):
                handler_target = handler.get("target")
                if handler_target:
                    handler_files.append(handler_target)
    elif isinstance(execution, list):
        for item in execution:
            if isinstance(item, dict):
                handler_target = item.get("target")
                if handler_target:
                    handler_files.append(handler_target)
    handler_names = set()
    for hf in handler_files:
        handler_names.add(hf)
        if hf.endswith(".js"):
            handler_names.add(hf[:-3] + ".ts")
        elif hf.endswith(".ts"):
            handler_names.add(hf[:-3] + ".js")
    common_files = [
        "package.json", "package-lock.json", "tsconfig.json",
        "make.json", ".npmrc", "icon.png", "icon.svg", "task.loc.json",
    ]
    all_candidates = sorted(handler_names) + common_files
    for rel_path in all_candidates:
        f_url = f"{TASKS_REPO}/{folder}/{rel_path}"
        dest = target / rel_path
        if dest.exists():
            continue
        try:
            req = urllib.request.Request(f_url, headers={"User-Agent": "ado-local/0.1.0"})
            resp = urllib.request.urlopen(req, timeout=15)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(resp.read())
        except Exception:
            pass
    for lang in ("", "en-US", "de-DE", "es-ES", "fr-FR", "it-IT",
                 "ja-JP", "ko-KR", "pt-BR", "ru-RU", "zh-CN", "zh-TW"):
        sname = "resources.resjson" if not lang else f"{lang}/resources.resjson"
        sfile = f"Strings/{sname}"
        dest = target / sfile
        if dest.exists():
            continue
        try:
            req = urllib.request.Request(f"{TASKS_REPO}/{folder}/{sfile}", headers={"User-Agent": "ado-local/0.1.0"})
            resp = urllib.request.urlopen(req, timeout=10)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(resp.read())
        except Exception:
            pass


def download_task(
    task_spec: str,
    cache_dir: Path,
    agent_version: str = DEFAULT_AGENT_VERSION,
    log_callback: Callable[[str], None] | None = None,
    azure_devops_token: str | None = None,
    azure_devops_org: str | None = None,
) -> Path | None:
    def _log(msg: str) -> None:
        if log_callback:
            log_callback(msg)
        else:
            print(msg, file=sys.stderr)

    if "@" in task_spec:
        task_name, version_spec = task_spec.split("@", 1)
    else:
        task_name, version_spec = task_spec, ""

    task_dir = cache_dir / task_name
    if task_dir.exists():
        existing = sorted(task_dir.iterdir(), reverse=True) if task_dir.is_dir() else []
        if existing:
            _log(f"  {task_spec} already cached")
            return task_dir / existing[0].name

    _log(f"  Resolving {task_spec}...")

    # Primary path: download complete task package from Azure DevOps API
    if azure_devops_token and azure_devops_org:
        result = _download_task_from_distributed_api(
            task_name, version_spec, task_dir,
            azure_devops_token, azure_devops_org, cache_dir, _log,
        )
        if result:
            return result

    # Fallback: try agent ZIP (tasks bundled with agent distribution)
    agent_zip = _download_agent(agent_version, cache_dir, log_callback)
    if agent_zip:
        _extract_agent_externals(agent_zip, cache_dir)
        versions = _find_task_in_zip(agent_zip, task_name)
        if versions:
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
                    _log(f"  Warning: requested v{version_spec}, found v{resolved_version}")
            cached = task_dir / resolved_version
            if not cached.exists():
                files = versions.get(resolved_version)
                if files:
                    cached.mkdir(parents=True, exist_ok=True)
                    for inner_path, data in files:
                        dest = cached / inner_path
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(data)
                    if not (cached / "task.json").exists():
                        _log(f"  Extracted task has no task.json")
                        shutil.rmtree(cached)
                        return None
            _log(f"  Cached: {task_name} {resolved_version}")
            return cached

    # Final fallback: download from GitHub source + npm install
    _log(f"  Trying GitHub source...")
    result = _download_task_from_github(task_name, task_dir, version_spec, _log, agent_zip=agent_zip)
    if result:
        _log(f"  Downloaded {task_name} from GitHub")
        return result

    _log(f"  Task '{task_name}' could not be downloaded from any source")
    return None


def download_all_tasks(
    task_specs: list[str],
    cache_dir: Path,
    azure_devops_token: str | None = None,
    azure_devops_org: str | None = None,
) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for spec in task_specs:
        path = download_task(spec, cache_dir,
                             azure_devops_token=azure_devops_token,
                             azure_devops_org=azure_devops_org)
        results[spec] = path is not None
    return results


def _adu_url(org: str, endpoint: str, project: str | None = None) -> str:
    if project:
        return f"https://dev.azure.com/{org}/{project}/_apis/{endpoint}"
    return f"https://dev.azure.com/{org}/_apis/{endpoint}"


def _extmgmt_url(org: str, endpoint: str) -> str:
    return f"https://extmgmt.dev.azure.com/{org}/_apis/{endpoint}"


def _adu_headers(token: str) -> dict[str, str]:
    b64 = base64.b64encode(f":{token}".encode()).decode()
    return {
        "Authorization": f"Basic {b64}",
        "User-Agent": "ado-local/0.1.0",
        "Content-Type": "application/json",
    }


def _list_installed_extensions(org: str, token: str) -> list[dict[str, Any]]:
    url = _extmgmt_url(org, "ExtensionManagement/InstalledExtensions?api-version=7.1-preview.1&includeInstallationIssues=true")
    req = urllib.request.Request(url, headers=_adu_headers(token))
    resp = urllib.request.urlopen(req, timeout=30)
    data = json.loads(resp.read())
    return data.get("value", [])


def _download_vsix_task_files(
    publisher: str,
    extension_name: str,
    extension_version: str,
    task_folder: str,
    task_dir: Path,
    resolved_version: str,
    token: str | None = None,
    org: str | None = None,
    _log: Callable[[str], None] | None = None,
) -> Path | None:
    """Download task files by extracting from Marketplace VSIX package."""

    vsix_urls = [
        f"https://marketplace.visualstudio.com/_apis/public/gallery/publisher/{publisher}/extension/{extension_name}/{extension_version}/vspackage",
    ]
    if org:
        vsix_urls.append(
            f"https://marketplace.visualstudio.com/_apis/gallery/publisher/{publisher}/extension/{extension_name}/{extension_version}/vspackage"
        )

    for vsix_url in vsix_urls:
        headers = {"User-Agent": "ado-local/0.1.0"}
        if token:
            b64 = base64.b64encode(f":{token}".encode()).decode()
            headers["Authorization"] = f"Basic {b64}"

        try:
            if _log:
                _log(f"    Downloading VSIX: {vsix_url}")
            req = urllib.request.Request(vsix_url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=120)
            vsix_data = resp.read()

            target = task_dir / resolved_version
            target.mkdir(parents=True, exist_ok=True)

            prefix = f"Extensions/{task_folder}/".lower()
            found = 0
            with zipfile.ZipFile(io.BytesIO(vsix_data)) as zf:
                for name in zf.namelist():
                    if name.endswith("/"):
                        continue
                    lower_name = name.lower()
                    if not lower_name.startswith(prefix):
                        continue
                    rel_path = name[len("Extensions/"):] if name.lower().startswith("extensions/") else name
                    parts = rel_path.split("/", 1)
                    if len(parts) >= 2:
                        rel_path = parts[1]
                    dest = target / rel_path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(zf.read(name))
                    found += 1

            if found == 0:
                if _log:
                    _log(f"    No task files found in VSIX under Extensions/{task_folder}/")
                shutil.rmtree(target)
                continue

            if _log:
                _log(f"    Extracted {found} files from VSIX")
            return target
        except urllib.error.HTTPError as e:
            if _log:
                _log(f"    VSIX download HTTP {e.code}")
            if token and e.code == 401:
                if _log:
                    _log(f"    PAT may not have Marketplace read scope")
            continue
        except Exception as e:
            if _log:
                _log(f"    VSIX download error: {e}")
            continue

    return None


def _try_fetch_task_via_distributed_api(
    contrib_id: str,
    token: str,
    org: str,
    project: str | None,
    task_dir: Path,
    resolved_version: str,
    _log: Callable[[str], None],
) -> Path | None:
    """Try to download task files using distributedtask/tasks API with task GUID from contribution."""
    import re
    guid_match = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", contrib_id)
    if not guid_match:
        return None
    task_guid = guid_match.group(0)

    target = task_dir / resolved_version
    target.mkdir(parents=True, exist_ok=True)

    # Try with project scope first, then org-wide
    for use_project in [project, None]:
        for endpoint in [f"distributedtask/tasks/{task_guid}/zip", f"distributedtask/tasks/{task_guid}/download"]:
            url = _adu_url(org, f"{endpoint}?api-version=7.1-preview.1", use_project)
            try:
                _log(f"    Downloading task ZIP: {url}")
                req = urllib.request.Request(url, headers=_adu_headers(token))
                resp = urllib.request.urlopen(req, timeout=60)
                zip_data = resp.read()
                with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
                    for name in zf.namelist():
                        if name.endswith("/"):
                            continue
                        dest = target / name
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(zf.read(name))
                _log(f"    Downloaded task files via distributed API")
                return target
            except urllib.error.HTTPError as e:
                _log(f"    HTTP {e.code} for {endpoint}")
            except Exception as e:
                _log(f"    Error: {e}")
            if target.exists():
                shutil.rmtree(target)
                target.mkdir(parents=True, exist_ok=True)

    if target.exists():
        shutil.rmtree(target)
    return None


def _extract_task_json_from_props(props: dict | None, ext_version: str = "") -> dict | None:
    """Extract task.json-compatible dict from task contribution properties."""
    if not props:
        return None
    ver = props.get("version")
    if ver is None or ver == {}:
        # Fall back to extension version string
        if ext_version:
            parts = ext_version.split(".")
            ver = {
                "Major": int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 1,
                "Minor": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
                "Patch": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
            }
        else:
            ver = {"Major": 1, "Minor": 0, "Patch": 0}
    elif isinstance(ver, (int, float)):
        ver = {"Major": int(ver), "Minor": 0, "Patch": 0}
    elif not isinstance(ver, dict):
        ver = {"Major": 1, "Minor": 0, "Patch": 0}
    task_json = dict(props)
    task_json["version"] = ver
    return task_json


def _download_extension_task_files(
    task_name: str,
    version_spec: str,
    task_dir: Path,
    base_uri: str,
    fallback_base_uri: str | None,
    task_folder: str,
    _log: Callable[[str], None],
    token: str = "",
    org: str = "",
    project: str | None = None,
    contrib_id: str = "",
    publisher_name: str = "",
    extension_name: str = "",
    extension_version: str = "",
    contrib_props: dict | None = None,
) -> Path | None:
    """Download task files from an extension's CDN baseUri or fallback methods."""

    # Try CDN URLs first
    task_json_urls = [
        f"{base_uri.rstrip('/')}/Extensions/{task_folder}/task.json",
        f"{base_uri.rstrip('/')}/extensions/{task_folder}/task.json",
        f"{base_uri.rstrip('/')}/{task_folder}/task.json",
        f"{base_uri.rstrip('/')}/Tasks/{task_folder}/task.json",
    ]
    if fallback_base_uri:
        task_json_urls.extend([
            f"{fallback_base_uri.rstrip('/')}/Microsoft.VisualStudio.Services.Task/{task_folder}/task.json",
            f"{fallback_base_uri.rstrip('/')}/Microsoft.VisualStudio.Services.Task/{task_folder}/task.json?api-version=7.1-preview.1",
        ])

    task_json_data = None
    used_base_uri = base_uri

    def _try_url(url: str, use_auth: bool = False) -> dict | None:
        headers = {"User-Agent": "ado-local/0.1.0"}
        if use_auth and token:
            headers.update(_adu_headers(token))
        try:
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=15)
            return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            _log(f"    HTTP {e.code} for {url}")
        except Exception as e:
            _log(f"    Error: {e}")
        return None

    for url in task_json_urls:
        _log(f"    Trying task.json URL: {url}")
        task_json_data = _try_url(url)
        if task_json_data:
            if fallback_base_uri and fallback_base_uri.rstrip('/') in url:
                used_base_uri = fallback_base_uri
            _log(f"    Got task.json (version={task_json_data.get('version', {})})")
            break
        if token:
            _log(f"    Retrying with auth...")
            task_json_data = _try_url(url, use_auth=True)
            if task_json_data:
                if fallback_base_uri and fallback_base_uri.rstrip('/') in url:
                    used_base_uri = fallback_base_uri
                _log(f"    Got task.json (version={task_json_data.get('version', {})})")
                break

    if not task_json_data:
        # Fallback: extract task.json from contribution properties (available via API)
        task_json_data = _extract_task_json_from_props(contrib_props, extension_version)
        if task_json_data:
            _log(f"    Got task.json from contribution properties (version={task_json_data.get('version', {})})")

    if not task_json_data:
        _log(f"    CDN URLs all failed, trying VSIX download...")
        # Try VSIX download from Marketplace gallery
        v = {"Major": 1, "Minor": 0, "Patch": 0}
        resolved_version = f"{v['Major']}.{v['Minor']}.{v['Patch']}"
        vsix_result = _download_vsix_task_files(
            publisher_name, extension_name, extension_version,
            task_folder, task_dir, resolved_version,
            token=token, org=org, _log=_log,
        )
        if vsix_result:
            # Read task.json from extracted files to get real version
            tj = vsix_result / "task.json"
            if tj.exists():
                try:
                    tj_data = json.loads(tj.read_text())
                    v_m = tj_data.get("version", {})
                    rv = f"{v_m.get('Major', 1)}.{v_m.get('Minor', 0)}.{v_m.get('Patch', 0)}"
                    if rv != resolved_version:
                        new_target = task_dir / rv
                        if not new_target.exists():
                            vsix_result.rename(new_target)
                            resolved_version = rv
                            vsix_result = new_target
                        else:
                            shutil.rmtree(vsix_result)
                            vsix_result = new_target
                except Exception:
                    pass
            _log(f"  Cached: {task_name} {resolved_version}")
            return vsix_result

        # Try distributedtask/tasks API
        if token and org and contrib_id:
            _log(f"    Trying distributedtask/tasks API...")
            dist_result = _try_fetch_task_via_distributed_api(
                contrib_id, token, org, project,
                task_dir, resolved_version, _log,
            )
            if dist_result:
                _log(f"  Cached: {task_name} {resolved_version}")
                return dist_result

        _log(f"    Could not fetch task.json from any source")
        return None

    v = task_json_data.get("version", {})
    resolved_version = f"{v.get('Major', 1)}.{v.get('Minor', 0)}.{v.get('Patch', 0)}"

    # If version_spec specified, try to match major
    if version_spec:
        major = version_spec.split(".")[0]
        if not resolved_version.startswith(major):
            _log(f"  Warning: requested v{version_spec}, extension has v{resolved_version}")

    target = task_dir / resolved_version
    if target.exists():
        return target

    target.mkdir(parents=True, exist_ok=True)

    # Write task.json
    (target / "task.json").write_text(json.dumps(task_json_data, indent=2))

    # List and download all task files from CDN
    _log(f"  Downloading {task_folder} files from extension...")

    # Collect files to download from execution handlers
    files_to_try = []
    for handler_name, handler_config in task_json_data.get("execution", {}).items():
        if isinstance(handler_config, dict):
            h_target = handler_config.get("target", "")
            if h_target:
                files_to_try.append(h_target)
            for key in ("workingDirectory", "argumentFormat"):
                val = handler_config.get(key, "")
                if val and val.endswith((".ps1", ".js", ".py", ".exe")):
                    files_to_try.append(val)

    # Common additional files to attempt
    common_files = ["package.json", "package-lock.json", "tsconfig.json", "icon.png", "task.ps1", "task.js"]
    files_to_try.extend(common_files)
    files_to_try = list(dict.fromkeys(f for f in files_to_try if f))  # deduplicate

    for fname in files_to_try:
        if not fname:
            continue
        try:
            file_url = f"{base_uri.rstrip('/')}/Extensions/{task_folder}/{fname}"
            dest = target / fname.replace("\\", "/")
            if dest.exists():
                continue
            req = urllib.request.Request(file_url, headers={"User-Agent": "ado-local/0.1.0"})
            resp = urllib.request.urlopen(req, timeout=30)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(resp.read())
        except Exception:
            if fallback_base_uri:
                try:
                    fb_url = f"{fallback_base_uri.rstrip('/')}/Microsoft.VisualStudio.Services.Task/{task_folder}/{fname}"
                    req = urllib.request.Request(fb_url, headers={"User-Agent": "ado-local/0.1.0"})
                    resp = urllib.request.urlopen(req, timeout=15)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(resp.read())
                except Exception:
                    pass

    _log(f"  Cached: {task_name} {resolved_version}")
    return target


def download_task_from_azure_devops(
    task_spec: str,
    cache_dir: Path,
    token: str,
    org: str,
    project: str | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> Path | None:
    def _log(msg: str) -> None:
        if log_callback:
            log_callback(msg)
        else:
            print(msg, file=sys.stderr)

    if "@" in task_spec:
        task_name, version_spec = task_spec.split("@", 1)
    else:
        task_name, version_spec = task_spec, ""

    task_dir = cache_dir / task_name
    if task_dir.exists():
        existing = sorted(task_dir.iterdir(), reverse=True) if task_dir.is_dir() else []
        if existing:
            _log(f"  {task_name} already cached")
            return task_dir / existing[0].name

    _log(f"  Searching Azure DevOps org '{org}' for extension containing task '{task_name}'...")

    try:
        extensions = _list_installed_extensions(org, token)
    except urllib.error.HTTPError as e:
        _log(f"  Failed to list extensions: HTTP {e.code}")
        if e.code == 401 or e.code == 403:
            _log(f"  Check that your PAT has 'Extensions (Read)' scope (vso.extension)")
        return None
    except Exception as e:
        _log(f"  Failed to list extensions: {e}")
        return None

    _log(f"  Found {len(extensions)} installed extensions")

    for ext in extensions:
        contributions = ext.get("contributions", [])
        base_uri = ext.get("baseUri", "")
        fallback_uri = ext.get("fallbackBaseUri")
        publisher = ext.get("publisherName", "")
        ext_name = ext.get("extensionName", "")
        _log(f"  Checking extension: {publisher}.{ext_name} ({len(contributions)} contributions)")

        for contrib in contributions:
            contrib_type = contrib.get("type", "")
            contrib_id = contrib.get("id", "")
            if contrib_type != "ms.vss-distributed-task.task":
                _log(f"    Skipping contribution {contrib_id} (type={contrib_type})")
                continue
            props = contrib.get("properties", {})
            folder = props.get("name", "")
            _log(f"    Task contribution: id={contrib_id}, folder={folder}")
            if not folder:
                _log(f"    No 'name' property in contribution properties")
                continue

            # Check task_name plausibly matches via common substrings
            ext_id = contrib_id.split(".")[1] if len(contrib_id.split(".")) >= 2 else ""
            matches = (
                _has_common_substring(task_name, folder, 4)
                or _has_common_substring(task_name, ext_id, 4)
                or _has_common_substring(task_name, ext_name, 4)
            )
            if not matches:
                _log(f"    Skipping (no common substring with task '{task_name}')")
                continue

            if base_uri:
                # Extract identity names from contribution ID
                # Format: {publisher}.{extensionName}.{taskId}
                parts = contrib_id.split(".")
                pub_id_name = publisher
                ext_id_name = ext_name
                if len(parts) >= 3:
                    pub_id_name = parts[0]
                    ext_id_name = parts[1]
                ext_ver = ext.get("version", ext.get("extensionVersion", ""))
                result = _download_extension_task_files(
                    task_name, version_spec, task_dir,
                    base_uri, fallback_uri, folder, _log,
                    token=token, org=org, project=project,
                    contrib_id=contrib_id,
                    publisher_name=pub_id_name, extension_name=ext_id_name,
                    extension_version=ext_ver,
                    contrib_props=props,
                )
                if result:
                    return result

    _log(f"  Task '{task_name}' not found in any installed extension")
    return None
