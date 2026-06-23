from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from ado_local.models.task import HandlerType, ResolvedTask, TaskExecution
from ado_local.models.pipeline import TaskStep, StepStatus
from ado_local.logging.command_parser import LoggingCommandProcessor
from ado_local.models.events import EventType, PipelineEvent, EventHandler


def execute_task(
    step: TaskStep,
    task: ResolvedTask,
    workspace_env: dict[str, str],
    variables: dict[str, Any],
    event_handler: Optional[EventHandler] = None,
) -> TaskStep:
    step.status = StepStatus.RUNNING
    step.start_time = time.time()
    _emit(event_handler, EventType.STEP_START, step_index=0, step_name=step.task)

    handlers = task.definition.get_handlers()
    if not handlers:
        step.status = StepStatus.FAILED
        step.logs.append("No supported handler found in task.json")
        step.end_time = time.time()
        _emit(event_handler, EventType.STEP_COMPLETE, step_index=0, step_name=step.task, status="failed")
        return step

    handler = handlers[0]
    task_path = Path(task.path)
    target_script = task_path / handler.target

    if not target_script.exists():
        step.status = StepStatus.FAILED
        step.logs.append(f"Handler target not found: {target_script}")
        step.end_time = time.time()
        _emit(event_handler, EventType.STEP_COMPLETE, step_index=0, step_name=step.task, status="failed")
        return step

    env = _build_environment(step, task, workspace_env, variables)
    cmd = _build_command(handler, target_script)

    step.logs.append(f"Running: {' '.join(cmd)}")
    _emit(event_handler, EventType.STEP_LOG, step_index=0, step_name=step.task, log_line=f"Running: {' '.join(cmd)}")

    command_processor = LoggingCommandProcessor()

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            cwd=str(task_path),
        )

        for line in iter(proc.stdout.readline, ""):
            line = line.rstrip("\n\r")
            step.logs.append(line)
            _emit(event_handler, EventType.STEP_LOG, step_index=0, step_name=step.task, log_line=line)
            cmd_result = command_processor.process_line(line)

        proc.wait()
        step.exit_code = proc.returncode

        if command_processor.variables:
            variables.update(command_processor.variables)
        if command_processor.errors:
            step.logs.extend([f"##vso error: {e}" for e in command_processor.errors])
            _emit(event_handler, EventType.ERROR, step_index=0, step_name=step.task, message="; ".join(command_processor.errors))
        if command_processor.warnings:
            step.logs.extend([f"##vso warning: {w}" for w in command_processor.warnings])
            _emit(event_handler, EventType.WARNING, step_index=0, step_name=step.task, message="; ".join(command_processor.warnings))

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
    _emit(
        event_handler,
        EventType.STEP_COMPLETE,
        step_index=0,
        step_name=step.task,
        status=step.status.value,
        duration=step.duration,
        exit_code=step.exit_code,
    )
    return step


def _build_command(handler: TaskExecution, target: Path) -> list[str]:
    if handler.handler_type in (HandlerType.NODE, HandlerType.NODE10, HandlerType.NODE20):
        return ["node", str(target)]
    elif handler.handler_type in (HandlerType.POWER_SHELL, HandlerType.POWER_SHELL2, HandlerType.POWER_SHELL3):
        return ["pwsh", "-File", str(target)]
    elif handler.handler_type == HandlerType.PROCESS:
        return [str(target)]
    return [str(target)]


def _build_environment(
    step: TaskStep,
    task: ResolvedTask,
    workspace_env: dict[str, str],
    variables: dict[str, Any],
) -> dict[str, str]:
    env = dict(os.environ)
    env.update(workspace_env)

    for key, value in variables.items():
        env_key = key.upper().replace(".", "_")
        env[env_key] = str(value)

    for input_name, input_value in step.inputs.items():
        env_key = f"INPUT_{input_name.upper()}"
        env[env_key] = str(input_value)

    for task_input in task.definition.inputs:
        input_name = task_input.name
        if f"INPUT_{input_name.upper()}" not in env:
            if task_input.default is not None:
                env[f"INPUT_{input_name.upper()}"] = str(task_input.default)

    for key, value in step.env.items():
        env[key] = str(value)

    return env


def _emit(
    handler: Optional[EventHandler],
    event_type: EventType,
    **kwargs: Any,
) -> None:
    if handler:
        handler(PipelineEvent(event_type=event_type, **kwargs))
