from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from ado_local.models.config import ServiceConnectionMapping
from ado_local.models.task import HandlerType, ResolvedTask, TaskExecution
from ado_local.models.pipeline import TaskStep, StepStatus
from ado_local.logging.command_parser import LoggingCommandProcessor
from ado_local.logging.ansi import normalize_log_line
from ado_local.models.events import EventType, PipelineEvent, EventHandler
from ado_local.parser.variable_expander import expand_variables


def execute_task(
    step: TaskStep,
    task: ResolvedTask,
    workspace_env: dict[str, str],
    variables: dict[str, Any],
    event_handler: Optional[EventHandler] = None,
    cancel_requested: Optional[Callable[[], bool]] = None,
    service_connections: dict[str, ServiceConnectionMapping] | None = None,
    azure_devops_token: str | None = None,
    azure_devops_org: str | None = None,
    azure_devops_project: str | None = None,
    step_index: int = 0,
) -> TaskStep:
    step.status = StepStatus.RUNNING
    step.start_time = time.time()

    handlers = task.definition.get_handlers()
    if not handlers:
        step.status = StepStatus.FAILED
        step.logs.append("No supported handler found in task.json")
        step.end_time = time.time()
        return step

    handler = handlers[0]
    task_path = Path(task.path)
    target = handler.target.replace("$(currentDirectory)", str(task_path))
    target_script = task_path / target

    if not target_script.exists():
        step.status = StepStatus.FAILED
        step.logs.append(f"Handler target not found: {target_script}")
        step.end_time = time.time()
        return step

    env = _build_environment(
        step, task, workspace_env, variables,
        service_connections, azure_devops_token, azure_devops_org, azure_devops_project,
    )
    cmd = _build_command(handler, target_script)
    task_cwd_raw = handler.working_directory or workspace_env.get("BUILD_SOURCESDIRECTORY") or str(task_path)
    task_cwd = Path(str(expand_variables(task_cwd_raw, variables))).resolve()

    step.logs.append(f"Running: {' '.join(cmd)} in {task_cwd}")
    _emit(event_handler, EventType.STEP_LOG, step_index=step_index, step_name=step.task, log_line=f"Running: {' '.join(cmd)} in {task_cwd}")

    command_processor = LoggingCommandProcessor()

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(task_cwd),
        )

        for line in iter(proc.stdout.readline, ""):
            if cancel_requested and cancel_requested():
                proc.kill()
                break
            line = normalize_log_line(line.rstrip("\n\r"))
            cmd_result = command_processor.process_line(line)
            if _should_suppress_logging_command(line, env):
                continue
            safe_line = command_processor.mask(line)
            step.logs.append(safe_line)
            _emit(event_handler, EventType.STEP_LOG, step_index=step_index, step_name=step.task, log_line=safe_line)

        proc.wait()
        step.exit_code = proc.returncode

        if cancel_requested and cancel_requested():
            step.status = StepStatus.CANCELLED
            step.logs.append("Cancelled by user")

        if command_processor.variables:
            variables.update(command_processor.variables)
        if command_processor.errors:
            step.logs.extend([f"##vso error: {e}" for e in command_processor.errors])
            _emit(event_handler, EventType.ERROR, step_index=step_index, step_name=step.task, message="; ".join(command_processor.errors))
        if command_processor.warnings:
            warning_summary = f"{len(command_processor.warnings)} warning(s) reported by logging commands"
            step.logs.append(warning_summary)
            _emit(event_handler, EventType.WARNING, step_index=step_index, step_name=step.task, message=warning_summary)

        if proc.returncode != 0:
            step.status = StepStatus.FAILED
            step.logs.append(f"Process exited with code {proc.returncode}")
        elif command_processor.task_result == "failed":
            step.status = StepStatus.FAILED
            step.logs.append("Task completed with result: failed")
        else:
            step.status = StepStatus.SUCCEEDED
            step.logs.append("Task completed successfully")

    except FileNotFoundError as e:
        step.status = StepStatus.FAILED
        step.logs.append(f"Executor not found: {e}")
    except Exception as e:
        step.status = StepStatus.FAILED
        step.logs.append(f"Execution failed: {e}")

    step.end_time = time.time()
    return step


def _should_suppress_logging_command(line: str, env: dict[str, str]) -> bool:
    lower = line.lower()
    if lower.startswith("##vso[task.debug]") and env.get("SYSTEM_DEBUG", "").lower() != "true":
        return True
    return (
        lower.startswith("##vso[task.issue")
        or lower.startswith("##vso[task.logissue")
        or lower.startswith("##vso[log.issue")
    )


def _build_command(handler: TaskExecution, target: Path) -> list[str]:
    target_abs = target.resolve()
    if handler.handler_type in (HandlerType.NODE, HandlerType.NODE10, HandlerType.NODE16, HandlerType.NODE20, HandlerType.NODE20_1):
        return ["node", str(target_abs)]
    elif handler.handler_type in (HandlerType.POWER_SHELL, HandlerType.POWER_SHELL2, HandlerType.POWER_SHELL3):
        sdk_dir = target_abs.parent / "ps_modules" / "VstsTaskSdk"
        if sdk_dir.is_dir():
            import uuid
            shim = (
                "function Get-VstsInput {\n"
                "  param([string]$Name,[switch]$Require,[string]$Default,[switch]$AsBool,[switch]$AsInt)\n"
                "  $k='INPUT_'+$Name.Replace(' ','_').ToUpperInvariant()\n"
                "  $envs=[Environment]::GetEnvironmentVariables()\n"
                "  $exists=$envs.Contains($k)\n"
                "  $v=[Environment]::GetEnvironmentVariable($k)\n"
                "  if(!$exists -and $Require){throw \"Input '$Name' required\"}\n"
                "  if(!$exists){$v=$Default}\n"
                "  if($AsBool){return $v -in '1','true'}\n"
                "  if($AsInt){return try{[int]$v}catch{0}}\n"
                "  $v\n"
                "}\n"
                "function Invoke-VstsTool{\n"
                "  param([string]$FileName,[string]$Arguments,[switch]$RequireExitCodeZero)\n"
                "  Write-Host \"##[command]$FileName $Arguments\"\n"
                "  $ef=$FileName-replace\"'\",\"''\"\n"
                "  iex \"& '$ef' $Arguments\"\n"
                "  if($RequireExitCodeZero -and $global:LASTEXITCODE -ne 0){\n"
                "    throw \"Exit code $global:LASTEXITCODE\"\n"
                "  }\n"
                "}\n"
                f". '{target_abs.as_posix()}'"
            )
            shim_dir = Path(os.environ.get("TEMP", os.environ.get("TMP", "/tmp"))) / ".ado-local"
            shim_dir.mkdir(parents=True, exist_ok=True)
            shim_path = shim_dir / f"shim_{uuid.uuid4().hex[:8]}.ps1"
            shim_path.write_text(shim, encoding="utf-8")
            return ["pwsh", "-NoProfile", "-File", str(shim_path)]
        return ["pwsh", "-File", str(target_abs)]
    elif handler.handler_type == HandlerType.PROCESS:
        return [str(target_abs)]
    return [str(target_abs)]


def _build_environment(
    step: TaskStep,
    task: ResolvedTask,
    workspace_env: dict[str, str],
    variables: dict[str, Any],
    service_connections: dict[str, ServiceConnectionMapping] | None = None,
    azure_devops_token: str | None = None,
    azure_devops_org: str | None = None,
    azure_devops_project: str | None = None,
) -> dict[str, str]:
    env = dict(os.environ)
    env.update(workspace_env)

    for key, value in variables.items():
        env_key = key.upper().replace(".", "_")
        env[env_key] = str(value)

    for input_name, input_value in step.inputs.items():
        if input_value is None:
            continue
        env_key = f"INPUT_{input_name.upper()}"
        env[env_key] = str(input_value)

    for task_input in task.definition.inputs:
        input_name = task_input.name
        if f"INPUT_{input_name.upper()}" not in env:
            if task_input.default is not None:
                env[f"INPUT_{input_name.upper()}"] = str(task_input.default)

    _apply_local_task_overrides(env, task)

    for key, value in step.env.items():
        env[key] = str(value)

    if azure_devops_token:
        env.setdefault("ENDPOINT_AUTH_SCHEME_SYSTEMVSSCONNECTION", "OAuth")
        env.setdefault(
            "ENDPOINT_AUTH_SYSTEMVSSCONNECTION",
            '{"parameters":{"AccessToken":"' + azure_devops_token + '"},"scheme":"OAuth"}',
        )
        env.setdefault("ENDPOINT_AUTH_PARAMETER_SYSTEMVSSCONNECTION_ACCESSTOKEN", azure_devops_token)
        env.setdefault("SYSTEM_ACCESSTOKEN", azure_devops_token)
    if azure_devops_org:
        collection_uri = f"https://dev.azure.com/{azure_devops_org}/"
        env.setdefault("SYSTEM_TEAMFOUNDATIONCOLLECTIONURI", collection_uri)
        env.setdefault("SYSTEM_COLLECTIONURI", collection_uri)
        env.setdefault("ENDPOINT_URL_SYSTEMVSSCONNECTION", collection_uri)
    if azure_devops_project:
        env.setdefault("SYSTEM_TEAMPROJECT", azure_devops_project)
    env.setdefault("SYSTEM_SERVERTYPE", "Hosted")
    env.setdefault("AGENT_VERSION", "4.275.0")

    _add_service_connection_env(env, step, service_connections or {}, azure_devops_token)

    # Task-lib uses these paths early during import. Keep them absolute even if
    # pipeline variables with dotted names also produced AGENT_* env vars.
    for key in (
        "AGENT_TEMPDIRECTORY",
        "AGENT_TOOLSDIRECTORY",
        "AGENT_WORKFOLDER",
        "AGENT_BUILDDIRECTORY",
        "BUILD_SOURCESDIRECTORY",
        "BUILD_STAGINGDIRECTORY",
        "BUILD_BINARIESDIRECTORY",
        "BUILD_ARTIFACTSTAGINGDIRECTORY",
        "SYSTEM_DEFAULTWORKINGDIRECTORY",
        "SYSTEM_ARTIFACTSDIRECTORY",
    ):
        value = env.get(key)
        if value:
            abs_path = str(Path(value).resolve())
            env[key] = abs_path
            if key.endswith("DIRECTORY") or key.endswith("FOLDER"):
                Path(abs_path).mkdir(parents=True, exist_ok=True)

    # Add agent externals to PATH so tasks can find bundled tools
    ext_dir = Path(task.path).parent.parent.parent / "_agent_externals"
    if ext_dir.exists():
        extra_paths = []
        # add bin/ and common tool directories
        for sub in ["", "node/bin", "node10/bin", "node16/bin", "node20_1/bin", "node24/bin"]:
            p = ext_dir / sub
            if p.is_dir():
                extra_paths.append(str(p))
        if extra_paths:
            existing = env.get("PATH", "")
            env["PATH"] = os.pathsep.join(extra_paths) + os.pathsep + existing

    return env


def _apply_local_task_overrides(env: dict[str, str], task: ResolvedTask) -> None:
    if task.name.lower() != "nugettoolinstaller":
        return
    version_spec = env.get("INPUT_VERSIONSPEC", "").strip()
    check_latest = env.get("INPUT_CHECKLATEST", "").strip().lower() == "true"
    if version_spec or not check_latest:
        return

    cached_version = _latest_cached_tool_version(env.get("AGENT_TOOLSDIRECTORY", ""), "NuGet")
    if not cached_version:
        return

    # Hosted agents can query dist.nuget.org for checkLatest. Local runs should
    # prefer the existing tool cache when no explicit version was requested.
    env["INPUT_VERSIONSPEC"] = cached_version
    env["INPUT_CHECKLATEST"] = "false"


def _latest_cached_tool_version(tool_dir: str, tool_name: str) -> str | None:
    if not tool_dir:
        return None
    root = Path(tool_dir) / tool_name
    if not root.is_dir():
        return None
    versions: list[tuple[tuple[int, ...], str]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            key = tuple(int(part) for part in child.name.split("."))
        except ValueError:
            continue
        if any(child.glob("*.complete")):
            versions.append((key, child.name))
    if not versions:
        return None
    versions.sort()
    return versions[-1][1]


def _add_service_connection_env(
    env: dict[str, str],
    step: TaskStep,
    service_connections: dict[str, ServiceConnectionMapping],
    azure_devops_token: str | None,
) -> None:
    for input_name, input_value in step.inputs.items():
        if not input_name.lower().endswith("serviceconnections"):
            continue
        for endpoint_name in _split_service_connections(str(input_value)):
            mapping = service_connections.get(endpoint_name)
            values = mapping.variables if mapping else {}
            endpoint_url = values.get("url") or (mapping.config if mapping else None) or endpoint_name
            scheme = (values.get("scheme") or (mapping.type if mapping else None) or "token").lower()
            if scheme in ("usernamepassword", "username_password", "basic"):
                auth_scheme = "UsernamePassword"
                params = {
                    "username": values.get("username") or values.get("user") or "",
                    "password": values.get("password") or values.get("token") or azure_devops_token or "local",
                }
            elif scheme in ("none", "apikey", "nugetkey"):
                auth_scheme = "None"
                params = {"nugetkey": values.get("nugetkey") or values.get("apikey") or values.get("token") or "local"}
            else:
                auth_scheme = "Token"
                params = {"apitoken": values.get("apitoken") or values.get("token") or azure_devops_token or "local"}

            env[f"ENDPOINT_URL_{endpoint_name}"] = endpoint_url
            env[f"ENDPOINT_AUTH_SCHEME_{endpoint_name}"] = auth_scheme
            env[f"ENDPOINT_AUTH_{endpoint_name}"] = '{"parameters":' + __import__("json").dumps(params) + ',"scheme":"' + auth_scheme + '"}'
            for key, value in params.items():
                env[f"ENDPOINT_AUTH_PARAMETER_{endpoint_name}_{key.upper()}"] = value


def _split_service_connections(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _emit(
    handler: Optional[EventHandler],
    event_type: EventType,
    **kwargs: Any,
) -> None:
    if handler:
        handler(PipelineEvent(event_type=event_type, **kwargs))
