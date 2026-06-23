from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from git import Repo, GitCommandError

from ado_local.models.pipeline import CheckoutStep, StepStatus
from ado_local.execution.workspace import WorkspaceManager
from ado_local.models.events import EventType, PipelineEvent

logger = logging.getLogger(__name__)


def _emit_log(event_handler: Any, step_idx: int, line: str) -> None:
    if event_handler:
        event_handler(PipelineEvent(event_type=EventType.STEP_LOG, step_index=step_idx, log_line=line))


def execute_checkout(
    step: CheckoutStep,
    workspace: WorkspaceManager,
    repo_url: str | None = None,
    branch: str | None = None,
    commit: str | None = None,
    event_handler: Any | None = None,
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

        clone_url = repo_url or _detect_repo_url()

        clone_args = ["git", "clone", "--progress"]
        if branch:
            clone_args.extend(["--branch", branch])
        clone_args.extend([clone_url, str(target)])

        line = f"Cloning {clone_url} into {target}"
        step.logs.append(line)
        _emit_log(event_handler, 0, line)

        proc = subprocess.Popen(
            clone_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        for line in iter(proc.stdout.readline, ""):
            line = line.rstrip("\n\r")
            if line:
                step.logs.append(line)
                _emit_log(event_handler, 0, line)

        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"git clone failed with exit code {proc.returncode}")

        repo = Repo(target)

        if commit:
            repo.git.checkout(commit)
            msg = f"Checked out commit {commit[:12]}"
            step.logs.append(msg)
            _emit_log(event_handler, 0, msg)

        if step.submodules:
            repo.submodule_update(init=True, recursive=True)
            msg = "Initialized submodules"
            step.logs.append(msg)
            _emit_log(event_handler, 0, msg)

        if step.lfs:
            repo.git.lfs("pull")
            msg = "Pulled LFS objects"
            step.logs.append(msg)
            _emit_log(event_handler, 0, msg)

        step.status = StepStatus.SUCCEEDED
        msg = f"Checkout complete"
        step.logs.append(msg)
        _emit_log(event_handler, 0, msg)
    except GitCommandError as e:
        step.status = StepStatus.FAILED
        msg = f"Git error: {e}"
        step.logs.append(msg)
        _emit_log(event_handler, 0, msg)
    except Exception as e:
        step.status = StepStatus.FAILED
        msg = f"Checkout failed: {e}"
        step.logs.append(msg)
        _emit_log(event_handler, 0, msg)
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
