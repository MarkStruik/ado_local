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
    PublishStep,
    StepStatus,
    JobStatus,
)
from ado_local.models.config import LocalSettings
from ado_local.models.events import EventType, PipelineEvent, EventHandler
from ado_local.execution.workspace import WorkspaceManager
from ado_local.execution.checkout import execute_checkout, detect_current_branch, detect_current_commit
from ado_local.execution.task_runner import execute_task
from ado_local.execution.task_runner import _should_suppress_logging_command
from ado_local.cache.task_cache import resolve_task
from ado_local.parser.variable_expander import expand_variables
from ado_local.parser.expression import eval_runtime_expression
from ado_local.logging.command_parser import LoggingCommandProcessor
from ado_local.logging.ansi import normalize_log_line


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
        self._evaluate_runtime_variables()

        self.workspace = WorkspaceManager(self.settings)
        work_dir = self.workspace.create()
        self.variables["System.DefaultWorkingDirectory"] = str(work_dir.resolve())
        pipeline.workspace_dir = str(work_dir.resolve())

        self._emit(EventType.PIPELINE_START)

        if pipeline.stages:
            self._execute_stages(pipeline)
        elif pipeline.jobs:
            self._execute_jobs(pipeline)
        elif pipeline.steps:
            self._execute_steps(pipeline, self.variables)

        self._emit(EventType.PIPELINE_COMPLETE, status="completed")
        return pipeline

    def _evaluate_runtime_variables(self) -> None:
        context = {"variables": self.variables, "parameters": self.params}
        for key, value in list(self.variables.items()):
            if isinstance(value, str) and "$[" in value:
                self.variables[key] = eval_runtime_expression(value, context, self.counters)
                context["variables"] = self.variables

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
        failed = False
        for step_idx, step in enumerate(job.steps):
            if not step.enabled:
                step.status = StepStatus.SKIPPED
                self._emit(EventType.STEP_START, step_index=step_idx, step_name=str(step))
                self._emit(EventType.STEP_LOG, step_index=step_idx, step_name=str(step), log_line="Step skipped (disabled)")
                self._emit(EventType.STEP_COMPLETE, step_index=step_idx, step_name=str(step), status="skipped")
                continue

            if self.cancel_requested() or failed:
                step.status = StepStatus.SKIPPED
                reason = "cancelled" if self.cancel_requested() else "previous step failed"
                self._emit(EventType.STEP_START, step_index=step_idx, step_name=str(step))
                self._emit(EventType.STEP_LOG, step_index=step_idx, step_name=str(step), log_line=f"Step skipped ({reason})")
                self._emit(EventType.STEP_COMPLETE, step_index=step_idx, step_name=str(step), status="skipped")
                continue

            self._emit(EventType.STEP_START, step_index=step_idx, step_name=str(step))
            step_vars = dict(variables)
            step_vars["step"] = step_idx
            # Add workspace variables for $(Build.SourcesDirectory) etc. expansion
            step_vars["Build.SourcesDirectory"] = str(self.workspace.sources_dir.resolve())
            step_vars["Build.StagingDirectory"] = str(self.workspace.staging_dir.resolve())
            step_vars["Build.BinariesDirectory"] = str(self.workspace.binaries_dir.resolve())
            step_vars["Build.ArtifactStagingDirectory"] = str(self.workspace.staging_dir.resolve())
            step_vars["Agent.TempDirectory"] = str(self.workspace.temp_dir.resolve())
            step_vars["Agent.ToolsDirectory"] = str(self.workspace.tool_dir.resolve())
            step_vars["System.DefaultWorkingDirectory"] = str(self.workspace.root.resolve())
            step_vars["System.ArtifactsDirectory"] = str(self.workspace.staging_dir.resolve())

            if isinstance(step, CheckoutStep):
                branch = detect_current_branch()
                commit = detect_current_commit()
                step = execute_checkout(
                    step, self.workspace,
                    branch=branch, commit=commit,
                    event_handler=self.event_handler,
                    cancel_requested=self.cancel_requested,
                    checkout_mode=self.settings.checkout_mode,
                    step_index=step_idx,
                )
                if step.status == StepStatus.FAILED and not step.continue_on_error:
                    failed = True
            elif isinstance(step, TaskStep):
                step.inputs = expand_variables(step.inputs, step_vars)
                resolved = self._resolve_task(step, step_vars)
                if resolved:
                    step = execute_task(
                        step, resolved, self.workspace.get_env(), step_vars,
                        self.event_handler, self.cancel_requested,
                        service_connections=self.settings.service_connections,
                        azure_devops_token=self.settings.azure_devops_token,
                        azure_devops_org=self.settings.azure_devops_org,
                        azure_devops_project=self.settings.azure_devops_project,
                        step_index=step_idx,
                    )
                else:
                    step.status = StepStatus.FAILED
                    step.logs.append(f"Task not found: {step.task}")
                    self._emit(EventType.ERROR, step_index=step_idx, step_name=str(step), message=f"Task not found: {step.task}")
                if step.status == StepStatus.FAILED and not step.continue_on_error:
                    failed = True
            elif isinstance(step, ScriptStep):
                step.script = expand_variables(step.script, step_vars)
                if step.working_directory:
                    step.working_directory = expand_variables(step.working_directory, step_vars)
                step = self._execute_script(step, step_vars, step_idx)
                if step.status == StepStatus.FAILED and not step.continue_on_error:
                    failed = True
            elif isinstance(step, PublishStep):
                step = self._execute_publish(step, step_vars, step_idx)
                if step.status == StepStatus.FAILED and not step.continue_on_error:
                    failed = True

            self._emit(EventType.STEP_COMPLETE, step_index=step_idx, step_name=str(step), status=step.status.value)

    def _execute_steps(self, pipeline: Pipeline, variables: dict[str, Any]) -> None:
        pipeline.jobs.append(Job(name="default", steps=pipeline.steps))
        self._execute_steps_in_job(pipeline.jobs[0], variables)

    def _resolve_task(self, step: TaskStep, variables: dict[str, Any]) -> Any:
        cache_dir = Path(self.settings.task_cache_dir)
        return resolve_task(
            step.task, cache_dir, auto_download=True,
            azure_devops_token=self.settings.azure_devops_token,
            azure_devops_org=self.settings.azure_devops_org,
            azure_devops_project=self.settings.azure_devops_project,
        )

    def _execute_script(self, step: ScriptStep, variables: dict[str, Any], step_idx: int) -> ScriptStep:
        import subprocess
        import os

        step.status = StepStatus.RUNNING
        step.start_time = time.time()

        env = dict(os.environ)
        env.update(self.workspace.get_env())

        cmd = ["pwsh", "-Command", step.script] if os.name == "nt" else ["bash", "-c", step.script]
        cwd = step.working_directory or self.workspace.sources_dir
        step.logs.append(f"Running: {' '.join(cmd)}{f' in {cwd}' if cwd else ''}")
        self._emit(EventType.STEP_LOG, step_index=step_idx, step_name=str(step), log_line=f"Running: {' '.join(cmd)}{f' in {cwd}' if cwd else ''}")

        command_processor = LoggingCommandProcessor()

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            for line in iter(proc.stdout.readline, ""):
                if self.cancel_requested():
                    proc.kill()
                    break
                line = normalize_log_line(line.rstrip("\n\r"))
                cmd_result = command_processor.process_line(line)
                if _should_suppress_logging_command(line, env):
                    continue
                safe_line = command_processor.mask(line)
                step.logs.append(safe_line)
                self._emit(EventType.STEP_LOG, step_index=step_idx, step_name=str(step), log_line=safe_line)

            proc.wait()
            step.exit_code = proc.returncode

            if self.cancel_requested():
                step.status = StepStatus.CANCELLED
                step.logs.append("Script cancelled by user")
            elif command_processor.variables:
                variables.update(command_processor.variables)
            if command_processor.errors:
                for e in command_processor.errors:
                    step.logs.append(f"##vso error: {e}")
                self._emit(EventType.ERROR, step_index=step_idx, step_name=str(step), message="; ".join(command_processor.errors))
            if command_processor.warnings:
                warning_summary = f"{len(command_processor.warnings)} warning(s) reported by logging commands"
                step.logs.append(warning_summary)
                self._emit(EventType.WARNING, step_index=step_idx, step_name=str(step), message=warning_summary)

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

    def _execute_publish(self, step: PublishStep, variables: dict[str, Any], step_idx: int) -> PublishStep:
        from ado_local.artifacts.publisher import ArtifactPublisher
        step.status = StepStatus.RUNNING
        step.start_time = time.time()

        source = expand_variables(step.publish, variables)
        name = expand_variables(step.artifact, variables)
        step.logs.append(f"Publishing '{name}' from {source}")
        self._emit(EventType.STEP_LOG, step_index=step_idx, step_name=str(step), log_line=f"Publishing '{name}' from {source}")

        try:
            publisher = ArtifactPublisher(self.settings)
            dest = publisher.publish(source, name)
            step.artifact_path = str(dest.resolve())
            step.status = StepStatus.SUCCEEDED
            step.logs.append(f"Published to {dest}")
            self._emit(EventType.STEP_LOG, step_index=step_idx, step_name=str(step), log_line=f"Published to {dest}")
        except Exception as e:
            step.status = StepStatus.FAILED
            step.logs.append(f"Publish failed: {e}")
            self._emit(EventType.ERROR, step_index=step_idx, step_name=str(step), message=f"Publish failed: {e}")

        step.end_time = time.time()
        return step

    def _emit(self, event_type: EventType, **kwargs: Any) -> None:
        if self.event_handler:
            self.event_handler(PipelineEvent(event_type=event_type, **kwargs))
