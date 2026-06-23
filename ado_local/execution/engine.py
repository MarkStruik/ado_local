from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Optional

from ado_local.models.pipeline import (
    Pipeline,
    Job,
    Stage,
    Step,
    TaskStep,
    CheckoutStep,
    ScriptStep,
    StepStatus,
    JobStatus,
)
from ado_local.models.config import LocalSettings
from ado_local.models.events import EventType, PipelineEvent, EventHandler
from ado_local.execution.workspace import WorkspaceManager
from ado_local.execution.checkout import execute_checkout, detect_current_branch, detect_current_commit
from ado_local.execution.task_runner import execute_task
from ado_local.cache.task_cache import resolve_task
from ado_local.parser.variable_expander import expand_variables
from ado_local.logging.command_parser import LoggingCommandProcessor


class PipelineEngine:
    def __init__(
        self,
        settings: LocalSettings,
        event_handler: Optional[EventHandler] = None,
        cancel_requested: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.settings = settings
        self.event_handler = event_handler
        self.cancel_requested = cancel_requested or (lambda: False)
        self.workspace: Optional[WorkspaceManager] = None
        self.variables: dict[str, Any] = {}
        self.params: dict[str, Any] = {}
        self.counters: dict[str, int] = {}

    def execute(self, pipeline: Pipeline, params: dict[str, Any] | None = None) -> Pipeline:
        self.params = params or {}
        self.variables = dict(pipeline.variables)
        self.variables.update(self.settings.variables)
        self.variables["parameters"] = {**pipeline.parameters, **self.params}

        self.workspace = WorkspaceManager(self.settings)
        work_dir = self.workspace.create()
        self.variables["System.DefaultWorkingDirectory"] = str(work_dir)
        pipeline.workspace_dir = str(work_dir)

        self._emit(EventType.PIPELINE_START)

        if pipeline.stages:
            self._execute_stages(pipeline)
        elif pipeline.jobs:
            self._execute_jobs(pipeline)
        elif pipeline.steps:
            self._execute_steps(pipeline, self.variables)

        self._emit(EventType.PIPELINE_COMPLETE, status="completed")
        return pipeline

    def _execute_stages(self, pipeline: Pipeline) -> None:
        for stage in pipeline.stages:
            self._emit(EventType.STAGE_START, step_name=stage.name)
            stage.status = JobStatus.RUNNING
            for job in stage.jobs:
                self._execute_job(job, stage.variables)
            stage.status = JobStatus.SUCCEEDED
            self._emit(EventType.STAGE_COMPLETE, step_name=stage.name)

    def _execute_jobs(self, pipeline: Pipeline) -> None:
        for job in pipeline.jobs:
            self._emit(EventType.JOB_START, step_name=job.name)
            job.status = JobStatus.RUNNING
            self._execute_job(job, pipeline.variables)
            job.status = JobStatus.SUCCEEDED
            self._emit(EventType.JOB_COMPLETE, step_name=job.name)

    def _execute_job(self, job: Job, inherited_vars: dict[str, Any]) -> None:
        job.start_time = time.time()
        merged_vars = {**inherited_vars, **job.variables, **self.variables}
        self._execute_steps_in_job(job, merged_vars)
        job.end_time = time.time()

    def _execute_steps_in_job(self, job: Job, variables: dict[str, Any]) -> None:
        for step_idx, step in enumerate(job.steps):
            if self.cancel_requested():
                step.status = StepStatus.CANCELLED
                self._emit(EventType.STEP_START, step_index=step_idx, step_name=str(step))
                self._emit(EventType.STEP_LOG, step_index=step_idx, step_name=str(step), log_line="Step skipped (cancelled)")
                self._emit(EventType.STEP_COMPLETE, step_index=step_idx, step_name=str(step), status="cancelled")
                continue

            self._emit(EventType.STEP_START, step_index=step_idx, step_name=str(step))
            step_vars = dict(variables)
            step_vars["step"] = step_idx

            if isinstance(step, CheckoutStep):
                branch = detect_current_branch()
                commit = detect_current_commit()
                step = execute_checkout(step, self.workspace, branch=branch, commit=commit, event_handler=self.event_handler)
            elif isinstance(step, TaskStep):
                step.inputs = expand_variables(step.inputs, step_vars)
                resolved = self._resolve_task(step, step_vars)
                if resolved:
                    step = execute_task(step, resolved, self.workspace.get_env(), step_vars, self.event_handler)
                else:
                    step.status = StepStatus.FAILED
                    step.logs.append(f"Task not found: {step.task}")
                    self._emit(EventType.ERROR, step_index=step_idx, step_name=str(step), message=f"Task not found: {step.task}")
            elif isinstance(step, ScriptStep):
                step.script = expand_variables(step.script, step_vars)
                step = self._execute_script(step, step_vars, step_idx)

            self._emit(EventType.STEP_COMPLETE, step_index=step_idx, step_name=str(step), status=step.status.value)

    def _execute_steps(self, pipeline: Pipeline, variables: dict[str, Any]) -> None:
        pipeline.jobs.append(Job(name="default", steps=pipeline.steps))
        self._execute_steps_in_job(pipeline.jobs[0], variables)

    def _resolve_task(self, step: TaskStep, variables: dict[str, Any]) -> Any:
        cache_dir = Path(self.settings.task_cache_dir)
        return resolve_task(step.task, cache_dir)

    def _execute_script(self, step: ScriptStep, variables: dict[str, Any], step_idx: int) -> ScriptStep:
        import subprocess
        import os

        step.status = StepStatus.RUNNING
        step.start_time = time.time()

        env = dict(os.environ)
        env.update(self.workspace.get_env())

        cmd = ["pwsh", "-Command", step.script] if os.name == "nt" else ["bash", "-c", step.script]
        step.logs.append(f"Running: {' '.join(cmd)}")
        self._emit(EventType.STEP_LOG, step_index=step_idx, step_name=str(step), log_line=f"Running: {' '.join(cmd)}")

        command_processor = LoggingCommandProcessor()

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                text=True,
            )

            for line in iter(proc.stdout.readline, ""):
                line = line.rstrip("\n\r")
                step.logs.append(line)
                self._emit(EventType.STEP_LOG, step_index=step_idx, step_name=str(step), log_line=line)
                cmd_result = command_processor.process_line(line)

            proc.wait()
            step.exit_code = proc.returncode

            if command_processor.variables:
                variables.update(command_processor.variables)
            if command_processor.errors:
                for e in command_processor.errors:
                    step.logs.append(f"##vso error: {e}")
                self._emit(EventType.ERROR, step_index=step_idx, step_name=str(step), message="; ".join(command_processor.errors))
            if command_processor.warnings:
                for w in command_processor.warnings:
                    step.logs.append(f"##vso warning: {w}")
                self._emit(EventType.WARNING, step_index=step_idx, step_name=str(step), message="; ".join(command_processor.warnings))

            if proc.returncode != 0:
                step.status = StepStatus.FAILED
                step.logs.append(f"Script exited with code {proc.returncode}")
            elif command_processor.task_result == "failed":
                step.status = StepStatus.FAILED
            else:
                step.status = StepStatus.SUCCEEDED

        except FileNotFoundError as e:
            step.status = StepStatus.FAILED
            step.logs.append(f"Script executor not found: {e}")
            self._emit(EventType.ERROR, step_index=step_idx, step_name=str(step), message=f"Script executor not found: {e}")
        except Exception as e:
            step.status = StepStatus.FAILED
            step.logs.append(f"Script failed: {e}")
            self._emit(EventType.ERROR, step_index=step_idx, step_name=str(step), message=f"Script failed: {e}")

        step.end_time = time.time()
        return step

    def _emit(self, event_type: EventType, **kwargs: Any) -> None:
        if self.event_handler:
            self.event_handler(PipelineEvent(event_type=event_type, **kwargs))
