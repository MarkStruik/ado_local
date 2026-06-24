from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from git import Repo, GitCommandError

from ado_local.models.pipeline import CheckoutStep, StepStatus
from ado_local.execution.workspace import WorkspaceManager
from ado_local.models.events import EventType, PipelineEvent
from ado_local.logging.ansi import normalize_log_line

logger = logging.getLogger(__name__)


def _emit_log(event_handler: Any, step_idx: int, line: str) -> None:
    if event_handler:
        event_handler(PipelineEvent(event_type=EventType.STEP_LOG, step_index=step_idx, log_line=line))


def _log(lines: list[str] | None, event_handler: Any | None, step_idx: int, line: str) -> None:
    if lines is not None:
        lines.append(line)
    _emit_log(event_handler, step_idx, line)


def _run_git(args: list[str], cwd: Path, event_handler: Any | None = None, lines: list[str] | None = None, step_idx: int = 0) -> None:
    proc = subprocess.Popen(
        ["git", *args],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    for line in iter(proc.stdout.readline, ""):
        line = normalize_log_line(line.rstrip("\n\r"))
        if line:
            _log(lines, event_handler, step_idx, line)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed with exit code {proc.returncode}")


def _git_output(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _local_clone_submodules(src: Path, target: Path, event_handler: Any | None = None, lines: list[str] | None = None, step_idx: int = 0) -> None:
    src = src.resolve()
    target = target.resolve()
    modules = target / ".gitmodules"
    if not modules.exists():
        return

    paths_raw = _git_output(["config", "--file", ".gitmodules", "--get-regexp", r"submodule\..*\.path"], target)
    for line in paths_raw.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        rel_path = parts[1].replace("/", os.sep)
        src_sub = src / rel_path
        dst_sub = target / rel_path
        if not (src_sub / ".git").exists():
            _log(lines, event_handler, step_idx, f"Skipping submodule {rel_path}: local source not initialized")
            continue

        commit = _git_output(["rev-parse", f"HEAD:{rel_path.replace(os.sep, '/')}"] , target)
        if dst_sub.exists():
            shutil.rmtree(dst_sub)
        dst_sub.parent.mkdir(parents=True, exist_ok=True)
        _log(lines, event_handler, step_idx, f"Local clone submodule {rel_path}")
        _run_git(["clone", "--local", "--progress", str(src_sub.resolve()), str(dst_sub.resolve())], target, event_handler, lines, step_idx)
        _run_git(["checkout", commit], dst_sub, event_handler, lines, step_idx)
        _local_clone_submodules(src_sub, dst_sub, event_handler, lines, step_idx)


def execute_checkout(
    step: CheckoutStep,
    workspace: WorkspaceManager,
    repo_url: str | None = None,
    branch: str | None = None,
    commit: str | None = None,
    event_handler: Any | None = None,
    cancel_requested: Optional[Callable[[], bool]] = None,
    checkout_mode: str = "local",
    step_index: int = 0,
) -> CheckoutStep:
    step.status = StepStatus.RUNNING
    step.start_time = __import__("time").time()
    target = workspace.sources_dir

    if step.checkout == "none":
        step.status = StepStatus.SUCCEEDED
        step.end_time = __import__("time").time()
        return step

    try:
        if target.exists():
            shutil.rmtree(target)

        if checkout_mode == "local":
            src = Path.cwd()
            ref = commit or branch or "HEAD"
            msg = f"Local clone from {src} into {target} (ref={ref[:40]})"
            step.logs.append(msg)
            _emit_log(event_handler, step_index, msg)
            clone_args = ["git", "clone", "--local", "--progress", str(src), str(target)]
            if branch:
                clone_args.insert(3, "--branch")
                clone_args.insert(4, branch)
            proc = subprocess.Popen(
                clone_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace",
            )
            for line in iter(proc.stdout.readline, ""):
                line = normalize_log_line(line.rstrip("\n\r"))
                if line:
                    step.logs.append(line)
                    _emit_log(event_handler, step_index, line)
            proc.wait()
            if proc.returncode != 0:
                raise RuntimeError(f"git clone failed with exit code {proc.returncode}")
            msg = f"Local clone complete"
            step.logs.append(msg)
            _emit_log(event_handler, step_index, msg)
        else:
            clone_url = repo_url or _detect_repo_url()
            clone_args = ["git", "clone", "--progress"]
            if branch:
                clone_args.extend(["--branch", branch])
            clone_args.extend([clone_url, str(target)])

            msg = f"Cloning {clone_url} ({branch or 'default'}) into {target}"
            step.logs.append(msg)
            _emit_log(event_handler, step_index, msg)

            proc = subprocess.Popen(
                clone_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            for line in iter(proc.stdout.readline, ""):
                if cancel_requested and cancel_requested():
                    proc.kill()
                    break
                line = normalize_log_line(line.rstrip("\n\r"))
                if line:
                    step.logs.append(line)
                    _emit_log(event_handler, step_index, line)

            proc.wait()
            if cancel_requested and cancel_requested():
                step.status = StepStatus.CANCELLED
                step.logs.append("Checkout cancelled by user")
                _emit_log(event_handler, step_index, "Checkout cancelled by user")
                step.end_time = __import__("time").time()
                return step
            if proc.returncode != 0:
                raise RuntimeError(f"git clone failed with exit code {proc.returncode}")

        repo = Repo(target)

        ref = commit or (branch or "")
        if ref:
            repo.git.checkout(ref)
            msg = f"Checked out {ref[:40]}"
            step.logs.append(msg)
            _emit_log(event_handler, step_index, msg)

        if step.submodules:
            if checkout_mode == "local":
                _local_clone_submodules(src, target, event_handler, step.logs, step_index)
            else:
                repo.git.submodule("update", "--init", "--recursive")
            msg = "Initialized submodules"
            step.logs.append(msg)
            _emit_log(event_handler, step_index, msg)

        if step.lfs:
            repo.git.lfs("pull")
            msg = "Pulled LFS objects"
            step.logs.append(msg)
            _emit_log(event_handler, step_index, msg)

        step.status = StepStatus.SUCCEEDED
        msg = f"Checkout complete"
        step.logs.append(msg)
        _emit_log(event_handler, step_index, msg)
    except GitCommandError as e:
        if step.status != StepStatus.CANCELLED:
            step.status = StepStatus.FAILED
        msg = f"Git error: {e}"
        step.logs.append(msg)
        _emit_log(event_handler, step_index, msg)
    except Exception as e:
        if step.status != StepStatus.CANCELLED:
            step.status = StepStatus.FAILED
        msg = f"Checkout failed: {e}"
        step.logs.append(msg)
        _emit_log(event_handler, step_index, msg)
    finally:
        step.end_time = __import__("time").time()

    return step


def _detect_repo_url() -> str:
    try:
        repo = Repo(Path.cwd(), search_parent_directories=True)
        remote = repo.remote()
        return list(remote.urls)[0]
    except Exception:
        return "."


def detect_git_remote() -> str | None:
    import subprocess
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def parse_azure_devops_remote(remote_url: str) -> tuple[str | None, str | None]:
    # https://dev.azure.com/{org}/{project}/_git/{repo}
    import re
    m = re.match(r"https://dev\.azure\.com/([^/]+)/([^/]+)/_git/", remote_url)
    if m:
        return m.group(1), m.group(2)
    # https://{org}.visualstudio.com/{project}/_git/{repo}
    m = re.match(r"https://([^.]+)\.visualstudio\.com/([^/]+)/_git/", remote_url)
    if m:
        return m.group(1), m.group(2)
    # git@ssh.dev.azure.com:v3/{org}/{project}/{repo}
    m = re.match(r"git@ssh\.dev\.azure\.com:v3/([^/]+)/([^/]+)/", remote_url)
    if m:
        return m.group(1), m.group(2)
    return None, None


def detect_current_branch() -> str | None:
    try:
        repo = Repo(Path.cwd(), search_parent_directories=True)
        if repo.head.is_detached:
            return None
        return repo.active_branch.name
    except Exception:
        return None


def detect_current_commit() -> str | None:
    try:
        repo = Repo(Path.cwd(), search_parent_directories=True)
        return repo.head.commit.hexsha
    except Exception:
        return None
