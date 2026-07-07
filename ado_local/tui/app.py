from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from rich.syntax import Syntax
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    LoadingIndicator,
    Log,
    RichLog,
    Static,
    Tree,
)
from textual.widgets.tree import TreeNode
from textual import events

from ado_local import __version__
from ado_local.analysis.analyzer import analyze_pipeline, AnalysisResult
from ado_local.models.config import LocalSettings, ServiceConnectionMapping


def _load_settings() -> LocalSettings:
    import json
    from pathlib import Path
    defaults = LocalSettings()
    settings_path = Path(defaults.workspace_root) / defaults.settings_file
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            return LocalSettings(**data)
        except Exception:
            pass
    return defaults
from ado_local.models.events import EventType, PipelineEvent
from ado_local.models.pipeline import (
    Pipeline,
    Job,
    Step,
    TaskStep,
    CheckoutStep,
    ScriptStep,
    PublishStep,
    StepStatus,
    JobStatus,
    RunRecord,
)
from ado_local.execution.engine import PipelineEngine
from ado_local.execution.checkout import detect_current_branch
from ado_local.parser.yaml_loader import load_pipeline_yaml
from ado_local.parser.expression import eval_runtime_expression
from ado_local.parser.pipeline import (
    collect_pipeline_tasks,
    load_and_compile_pipeline,
    parse_pipeline_model,
)


PIPELINE_PATTERNS = [
    "*.yml",
    "*.yaml",
]


def discover_pipelines(root: Path) -> list[Path]:
    found: list[Path] = []
    for pattern in PIPELINE_PATTERNS:
        matches = list(root.glob(pattern))
        found.extend(matches)
    for subdir in ["pipelines", ".ado", "build", "ci", ".azuredevops"]:
        sub_path = root / subdir
        if sub_path.is_dir():
            for pattern in PIPELINE_PATTERNS:
                matches = list(sub_path.glob(pattern))
                found.extend(matches)
    seen = set()
    unique = []
    for p in found:
        resolved = p.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    unique.sort(key=lambda p: (not _is_azure_pipeline(p), p.name.lower()))
    return unique


def _is_azure_pipeline(path: Path) -> bool:
    name = path.name.lower()
    if name in ("azure-pipelines.yml", "azure-pipelines.yaml", "azure-pipelines-main.yml"):
        return True
    try:
        data = load_pipeline_yaml(path)
        if isinstance(data, dict):
            has_steps = "steps" in data or "jobs" in data or "stages" in data
            has_task_ref = any(
                isinstance(v, str) and "@" in v
                for step in data.get("steps", [])
                if isinstance(step, dict)
                for v in step.values()
                if isinstance(v, str)
            )
            return has_steps or has_task_ref or "trigger" in data or "pool" in data
    except Exception:
        pass
    return False


def _run_history_dir() -> Path:
    return Path.home() / ".ado-local" / "run-history"


def _run_history_key(path: str) -> str:
    import hashlib
    return hashlib.md5(path.encode()).hexdigest()


def _list_run_records(pipeline_path: str) -> list[RunRecord]:
    records: list[RunRecord] = []
    try:
        key = _run_history_key(pipeline_path)
        history_dir = _run_history_dir() / key
        if history_dir.is_dir():
            for f in sorted(history_dir.glob("*.json"), reverse=True):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    records.append(RunRecord(**data))
                except Exception:
                    pass
    except Exception:
        pass
    return records


def _run_record_file(rec: RunRecord) -> Path | None:
    key = _run_history_key(rec.pipeline_path)
    history_dir = _run_history_dir() / key
    candidates = [history_dir / f"{rec.timestamp:.0f}.json", history_dir / f"{rec.timestamp}.json"]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    if history_dir.is_dir():
        for file in history_dir.glob("*.json"):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if abs(float(data.get("timestamp", -1)) - rec.timestamp) < 1:
                return file
    return None


def _safe_remove_work_path(path: str | Path | None) -> None:
    if not path:
        return
    try:
        import shutil

        target = Path(path).resolve()
        if not target.is_dir():
            return
        parts = {part.lower() for part in target.parts}
        if ".ado-local" not in parts or "work" not in parts or not target.name.startswith("run-"):
            return
        shutil.rmtree(target, ignore_errors=True)
    except Exception:
        pass


def _remove_work_path_async(path: str | Path | None) -> None:
    threading.Thread(target=_safe_remove_work_path, args=(path,), daemon=True).start()


def _delete_run_record(rec: RunRecord) -> None:
    try:
        ts_file = _run_record_file(rec)
        if ts_file and ts_file.exists():
            ts_file.unlink()
        _remove_work_path_async(rec.workspace_path)
    except Exception:
        pass


def _all_history_workspace_paths() -> set[Path]:
    paths: set[Path] = set()
    root = _run_history_dir()
    if not root.is_dir():
        return paths
    for file in root.glob("*/*.json"):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
            rec = RunRecord(**data)
            workspace = rec.workspace_path
            if not workspace and rec.pipeline_json:
                pipeline = Pipeline.model_validate_json(rec.pipeline_json)
                workspace = pipeline.workspace_dir
            if workspace:
                paths.add(Path(workspace).resolve())
        except Exception:
            continue
    return paths


def _cleanup_orphan_workspaces() -> None:
    try:
        settings = _load_settings()
        work_root = (Path(settings.workspace_root).resolve() / "work")
        if not work_root.is_dir():
            return
        referenced = _all_history_workspace_paths()
        for run_dir in work_root.iterdir():
            if run_dir.is_dir() and run_dir.name.startswith("run-") and run_dir.resolve() not in referenced:
                _safe_remove_work_path(run_dir)
    except Exception:
        pass


def _cleanup_orphan_workspaces_async() -> None:
    threading.Thread(target=_cleanup_orphan_workspaces, daemon=True).start()


def _format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return ""
    total = max(0, int(round(float(seconds))))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _copy_text_to_clipboard(text: str) -> None:
    import subprocess

    try:
        subprocess.run(
            ["pwsh", "-NoProfile", "-Command", "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
            input=text,
            text=True,
            encoding="utf-8",
            check=True,
        )
    except Exception:
        subprocess.run(["clip"], input=text.encode("utf-16le"), check=True)


def _open_path_location(path: str) -> None:
    import os
    import subprocess
    import sys

    target = Path(path)
    if target.is_file():
        target = target.parent
    if os.name == "nt":
        os.startfile(str(target))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])


def _artifact_path_from_logs(logs: list[str]) -> str | None:
    for line in reversed(logs):
        if line.startswith("Published artifact:"):
            path = line.split(":", 1)[1].strip()
            return path or None
    return None


def _pipeline_artifacts(pipeline: Pipeline) -> list[tuple[str, str]]:
    artifacts: list[tuple[str, str]] = []
    for job in pipeline.jobs:
        for step in job.steps:
            if isinstance(step, PublishStep):
                path = step.artifact_path or _artifact_path_from_logs(step.logs)
                if path:
                    name = Path(path).name or step.artifact or path
                    artifacts.append((name, path))
    return artifacts

def _save_run_record(pipeline_info: dict, pipeline: Pipeline, duration: float,
                      preflight_logs: list[str]) -> None:
    try:
        rec = RunRecord(
            pipeline_name=pipeline_info.get("name", pipeline.name or ""),
            pipeline_path=pipeline_info.get("path", ""),
            timestamp=time.time(),
            duration=duration,
            pipeline_json=pipeline.model_dump_json(),
            preflight_logs=preflight_logs,
            workspace_path=pipeline.workspace_dir,
        )
        key = _run_history_key(rec.pipeline_path)
        history_dir = _run_history_dir() / key
        history_dir.mkdir(parents=True, exist_ok=True)
        (history_dir / f"{rec.timestamp:.0f}.json").write_text(rec.model_dump_json(indent=2), encoding="utf-8")
    except Exception:
        pass


class RunHistoryScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("r", "new_run", "New Run"),
        Binding("a", "open_artifact", "Open Artifact"),
        Binding("j", "cursor_down", "Down"),
        Binding("k", "cursor_up", "Up"),
        Binding("d", "delete_run", "Delete"),
    ]

    def action_cursor_down(self) -> None:
        table = self.query_one("#history-table", DataTable)
        if table.cursor_row is None:
            table.move_cursor(row=0)
        elif table.cursor_row < table.row_count - 1:
            table.move_cursor(row=table.cursor_row + 1)

    def action_cursor_up(self) -> None:
        table = self.query_one("#history-table", DataTable)
        if table.cursor_row is None:
            table.move_cursor(row=0)
        elif table.cursor_row > 0:
            table.move_cursor(row=table.cursor_row - 1)

    def __init__(self, pipeline_info: dict[str, Any]) -> None:
        super().__init__()
        self._pipeline_info = pipeline_info
        self._history_artifacts: dict[int, list[tuple[str, str]]] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Container(
            Static(f"[bold]Run History:[/] {self._pipeline_info['name']}", id="history-title"),
            Horizontal(
                Static(" New Run ", id="new-run", classes="raw-log-action success-action"),
                Static(" Open Artifact ", id="open-artifact", classes="raw-log-action"),
                id="history-actions",
            ),
            DataTable(id="history-table"),
            Static(" Back ", id="back", classes="raw-log-action"),
            id="history-container",
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#history-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("#", "Status", "Duration", "Artifacts", "Date")
        self._populate_history_table(table)
        records = _list_run_records(self._pipeline_info["path"])
        if records:
            table.move_cursor(row=0)
        table.focus()

    def _populate_history_table(self, table: DataTable) -> None:
        self._history_artifacts.clear()
        records = _list_run_records(self._pipeline_info["path"])
        for i, rec in enumerate(records, 1):
            try:
                p = Pipeline.model_validate_json(rec.pipeline_json)
                all_ok = all(s.status == StepStatus.SUCCEEDED for job in p.jobs for s in job.steps)
                status = "[green]SUCCEEDED[/]" if all_ok else "[red]FAILED[/]"
                artifacts = _pipeline_artifacts(p)
            except Exception:
                status = "[dim]UNKNOWN[/]"
                artifacts = []
            if artifacts:
                self._history_artifacts[i - 1] = artifacts
            artifact_text = f"📦 {len(artifacts)}" if len(artifacts) > 1 else "📦" if artifacts else ""
            dur = _format_duration(rec.duration)
            date_str = datetime.fromtimestamp(rec.timestamp).strftime("%Y-%m-%d %H:%M")
            table.add_row(str(i), status, dur, artifact_text, date_str)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "new-run":
            self._start_new_run()
        elif event.button.id == "back":
            self.app.pop_screen()

    def on_click(self, event: events.Click) -> None:
        if getattr(event.widget, "id", None) == "new-run":
            self._start_new_run()
        elif getattr(event.widget, "id", None) == "open-artifact":
            self.action_open_artifact()
        elif getattr(event.widget, "id", None) == "back":
            self.app.pop_screen()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_new_run(self) -> None:
        self._start_new_run()

    def _start_new_run(self) -> None:
        if self._pipeline_info.get("parameters") or self._pipeline_info.get("variables"):
            self.app.push_screen(ParameterScreen(self._pipeline_info))
        else:
            self.app.push_screen(RunScreen(self._pipeline_info))

    def action_delete_run(self) -> None:
        table = self.query_one("#history-table", DataTable)
        if table.row_count == 0:
            return
        idx = table.cursor_row
        if idx is None:
            idx = 0
        records = _list_run_records(self._pipeline_info["path"])
        if 0 <= idx < len(records):
            rec = records[idx]
            _delete_run_record(rec)
            table.clear()
            self._populate_history_table(table)
            records = _list_run_records(self._pipeline_info["path"])
            if records:
                table.move_cursor(row=min(idx, len(records) - 1))

    def action_open_artifact(self) -> None:
        table = self.query_one("#history-table", DataTable)
        idx = table.cursor_row
        if idx is None:
            idx = 0
        artifacts = self._history_artifacts.get(idx, [])
        if artifacts:
            _open_path_location(artifacts[0][1])

    def action_view_run(self) -> None:
        table = self.query_one("#history-table", DataTable)
        if table.row_count == 0:
            return
        idx = table.cursor_row
        if idx is None and table.row_count > 0:
            idx = 0
            table.move_cursor(row=0)
        records = _list_run_records(self._pipeline_info["path"])
        if 0 <= idx < len(records):
            rec = records[idx]
            try:
                pipeline = Pipeline.model_validate_json(rec.pipeline_json)
                pipeline_info = parse_pipeline_info(Path(rec.pipeline_path))
                self.app.push_screen(RunResultScreen(pipeline, rec.duration, rec.preflight_logs,
                                                       double_pop=False, pipeline_info=pipeline_info,
                                                       run_timestamp=rec.timestamp))
            except Exception as e:
                self.query_one("#history-container", Container).mount(
                    Static(f"[red]Failed to load run: {e}[/]")
                )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_view_run()


def parse_pipeline_info(path: Path) -> dict[str, Any]:
    try:
        data = load_pipeline_yaml(path)
    except Exception as e:
        return {"name": path.name, "path": str(path), "parameters": [], "variables": [], "error": str(e)}
    trigger = data.get("trigger", {})
    trigger_name = ""
    if isinstance(trigger, dict):
        trigger_name = trigger.get("name", "") or ""
    name = data.get("name") or trigger_name or path.stem
    params_raw = data.get("parameters", {})
    params = []
    if isinstance(params_raw, list):
        for p in params_raw:
            if isinstance(p, dict):
                params.append({
                    "name": p.get("name", ""),
                    "display": p.get("displayName") or p.get("name", ""),
                    "type": p.get("type", "string"),
                    "default": p.get("default"),
                    "required": p.get("required", False),
                    "values": p.get("values", []),
                })
    elif isinstance(params_raw, dict):
        for key, val in params_raw.items():
            if isinstance(val, dict):
                params.append({
                    "name": key,
                    "display": val.get("displayName", key),
                    "type": val.get("type", "string"),
                    "default": val.get("default"),
                    "required": val.get("required", False),
                    "values": val.get("values", []),
                })
            else:
                params.append({
                    "name": key,
                    "display": key,
                    "type": "string",
                    "default": val,
                    "required": False,
                    "values": [],
                })
    variables = _parse_pipeline_variables(data.get("variables", {}))
    return {
        "name": name,
        "path": str(path),
        "parameters": params,
        "variables": variables,
        "error": None,
    }


def _parse_pipeline_variables(raw: Any) -> list[dict[str, Any]]:
    variables: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        for key, val in raw.items():
            variables.append({"name": str(key), "value": val, "readonly": False})
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            if "name" in item:
                variables.append({
                    "name": str(item.get("name", "")),
                    "value": item.get("value", ""),
                    "readonly": bool(item.get("readonly", False)),
                })
    return variables


def _pipeline_jobs_with_stage(pipeline: Pipeline) -> list[tuple[str | None, Job]]:
    if pipeline.stages:
        return [
            (stage.display_name or stage.name, job)
            for stage in pipeline.stages
            for job in stage.jobs
        ]
    return [(None, job) for job in pipeline.jobs]


def _pipeline_steps_flat(pipeline: Pipeline) -> list[Step]:
    return [step for _, job in _pipeline_jobs_with_stage(pipeline) for step in job.steps]


def _replace_pipeline_step(pipeline: Pipeline, old: Step, new: Step) -> None:
    for _, job in _pipeline_jobs_with_stage(pipeline):
        for idx, step in enumerate(job.steps):
            if step is old:
                job.steps[idx] = new
                return


def _step_display_name(step: Step, fallback: str = "step") -> str:
    display = getattr(step, "display_name", None)
    if display:
        return str(display)
    if isinstance(step, TaskStep):
        return step.task
    if isinstance(step, CheckoutStep):
        return f"checkout: {step.checkout}"
    if isinstance(step, ScriptStep):
        return "script"
    if isinstance(step, PublishStep):
        return f"publish: {step.publish}"
    return fallback


class PipelineSelectScreen(Screen):
    BINDINGS = [
        Binding("r", "run", "Run"),
        Binding("a", "analyze", "Analyze"),
        Binding("f5", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Container(
            Static("[bold blue]ado-local[/] -- Pipeline Runner", id="title"),
            Static(f"v{__version__}", id="version"),
            Static("Select a pipeline to run:", id="prompt"),
            ListView(id="pipeline-list"),
            Static("", id="pipeline-info"),
            Horizontal(
                Static(" Analyze ", id="analyze", classes="raw-log-action"),
                Static(" Run ", id="run", classes="raw-log-action success-action"),
                Static(" Refresh ", id="refresh", classes="raw-log-action"),
                Static(" Quit ", id="quit", classes="raw-log-action danger-action"),
                id="buttons",
            ),
            id="main-container",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._pipeline_info: list[dict[str, Any]] = []
        self._selected_index: int | None = None
        self._load_pipelines()
        list_view = self.query_one("#pipeline-list", ListView)
        list_view.focus()

    def _load_pipelines(self) -> None:
        root = Path.cwd()
        pipelines = discover_pipelines(root)
        list_view = self.query_one("#pipeline-list", ListView)
        info_static = self.query_one("#pipeline-info", Static)
        list_view.clear()
        self._pipeline_info = []

        if not pipelines:
            list_view.append(ListItem(Static("[red]No pipeline files found.[/]")))
            info_static.update("[yellow]Create azure-pipelines.yml in this directory[/]")
            self.query_one("#run", Static).add_class("disabled-action")
            self.query_one("#analyze", Static).add_class("disabled-action")
            return

        self.query_one("#run", Static).remove_class("disabled-action")
        self.query_one("#analyze", Static).remove_class("disabled-action")

        for p in pipelines:
            info = parse_pipeline_info(p)
            self._pipeline_info.append(info)
            label = f"[bold]{info['name']}[/]"
            if info.get("parameters"):
                label += f"  [{len(info['parameters'])} params]"
            if info.get("variables"):
                label += f"  [{len(info['variables'])} vars]"
            label += f"\n  [dim]{info['path']}[/]"
            list_view.append(ListItem(Static(label)))

        list_view.index = 0
        self._show_info(0)

    def _show_info(self, index: int) -> None:
        if 0 <= index < len(self._pipeline_info):
            info = self._pipeline_info[index]
            info_text = f"[bold]Pipeline:[/] {info['name']}\n[dim]{info['path']}[/]"
            if info.get("parameters"):
                info_text += "\n\n[bold]Parameters:[/]"
                for p in info["parameters"]:
                    req = "[red]*[/]" if p["required"] else ""
                    default = f" (default: {p['default']})" if p["default"] is not None else ""
                    values = f" [{', '.join(p['values'])}]" if p.get("values") else ""
                    info_text += f"\n  {req}[bold]{p['name']}[/]{values}{default}"
            if info.get("variables"):
                info_text += "\n\n[bold]Variables:[/]"
                for v in info["variables"]:
                    value = f" = {v.get('value')}" if v.get("value") is not None else ""
                    readonly = " [dim](readonly)[/]" if v.get("readonly") else ""
                    info_text += f"\n  [bold]{v['name']}[/]{value}{readonly}"
            if info.get("error"):
                info_text += f"\n\n[red]Error: {info['error']}[/]"
            self.query_one("#pipeline-info", Static).update(info_text)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is not None:
            index = event.list_view.index
            if index is not None:
                self._selected_index = index
                self._show_info(index)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self._run_selected()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run":
            self._run_selected()
        elif event.button.id == "analyze":
            self._analyze_selected()
        elif event.button.id == "refresh":
            self._load_pipelines()
        elif event.button.id == "quit":
            self.app.exit()

    def on_click(self, event: events.Click) -> None:
        widget_id = getattr(event.widget, "id", None)
        if widget_id == "run":
            self._run_selected()
        elif widget_id == "analyze":
            self._analyze_selected()
        elif widget_id == "refresh":
            self._load_pipelines()
        elif widget_id == "quit":
            self.app.exit()

    def _run_selected(self) -> None:
        if self._selected_index is not None and self._selected_index < len(self._pipeline_info):
            info = self._pipeline_info[self._selected_index]
            self.app.push_screen(RunHistoryScreen(info))

    def _analyze_selected(self) -> None:
        if self._selected_index is not None and self._selected_index < len(self._pipeline_info):
            info = self._pipeline_info[self._selected_index]
            self.app.push_screen(AnalyzeScreen(info))

    def action_run(self) -> None:
        self._run_selected()

    def action_analyze(self) -> None:
        self._analyze_selected()

    def action_refresh(self) -> None:
        self._load_pipelines()

    def action_quit(self) -> None:
        self.app.exit()


class ParameterScreen(Screen):
    BINDINGS = [
        Binding("enter", "run", "Run"),
        Binding("escape", "back", "Back"),
    ]

    def __init__(self, pipeline_info: dict[str, Any]) -> None:
        super().__init__()
        self._pipeline_info = pipeline_info

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Container(
            Static(f"[bold]Run inputs for:[/] {self._pipeline_info['name']}", id="param-title"),
            VerticalScroll(id="param-fields"),
            Horizontal(
                Static(" Preview ", id="run-params", classes="raw-log-action success-action"),
                Static(" Back ", id="back", classes="raw-log-action"),
                id="param-buttons",
            ),
            id="param-container",
        )
        yield Footer()

    def on_mount(self) -> None:
        fields = self.query_one("#param-fields", VerticalScroll)
        if self._pipeline_info.get("parameters"):
            fields.mount(Static("[bold cyan]Parameters[/]"))
        for idx, p in enumerate(self._pipeline_info.get("parameters", [])):
            req = "[red]*[/] " if p["required"] else ""
            label = f"{req}[bold]{p['name']}[/] ({p['type']})"
            if p.get("values"):
                label += f"  choices: {', '.join(p['values'])}"
            fields.mount(Static(label))
            default = p.get("default") or ""
            inp = Input(
                placeholder=str(default),
                id=f"param-{idx}",
                value=str(default) if default else "",
            )
            fields.mount(inp)
        if self._pipeline_info.get("variables"):
            fields.mount(Static("[bold cyan]Variables[/]"))
        for idx, v in enumerate(self._pipeline_info.get("variables", [])):
            readonly = " [dim](readonly)[/]" if v.get("readonly") else ""
            fields.mount(Static(f"[bold]{v['name']}[/]{readonly}"))
            value = "" if v.get("value") is None else str(v.get("value"))
            fields.mount(Input(placeholder=value, id=f"var-{idx}", value=value))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run-params":
            self._do_run()
        elif event.button.id == "back":
            self.app.pop_screen()

    def on_click(self, event: events.Click) -> None:
        widget_id = getattr(event.widget, "id", None)
        if widget_id == "run-params":
            self._do_run()
        elif widget_id == "back":
            self.app.pop_screen()

    def _do_run(self) -> None:
        params = {}
        for idx, p in enumerate(self._pipeline_info.get("parameters", [])):
            inp = self.query_one(f"#param-{idx}", Input)
            val = inp.value if inp.value else p.get("default")
            if val is not None:
                params[p["name"]] = val
        variables = {}
        for idx, v in enumerate(self._pipeline_info.get("variables", [])):
            inp = self.query_one(f"#var-{idx}", Input)
            val = inp.value if inp.value else v.get("value")
            if val is not None:
                variables[v["name"]] = val
        info = {**self._pipeline_info, "resolved_params": params, "resolved_variables": variables}
        self.app.push_screen(RunScreen(info))

    def action_run(self) -> None:
        self._do_run()

    def action_back(self) -> None:
        self.app.pop_screen()


class RuntimeVariablesDialog(ModalScreen[dict[str, str] | None]):
    def __init__(self, variables: list[dict[str, str]]) -> None:
        super().__init__()
        self._variables = variables

    def compose(self) -> ComposeResult:
        yield Container(
            Static("[bold]Runtime variables[/]", id="runtime-vars-title"),
            Static("Review evaluated formulas before this run.", classes="muted"),
            VerticalScroll(id="runtime-vars-fields"),
            Horizontal(
                Static(" Continue ", id="runtime-vars-run", classes="raw-log-action success-action"),
                Static(" Cancel ", id="runtime-vars-cancel", classes="raw-log-action danger-action"),
                id="runtime-vars-buttons",
            ),
            id="runtime-vars-dialog",
        )

    def on_mount(self) -> None:
        fields = self.query_one("#runtime-vars-fields", VerticalScroll)
        for item in self._variables:
            name = item["name"]
            fields.mount(Static(f"[bold]{name}[/] = {item['expression']}"))
            fields.mount(Input(value=item["value"], id=f"runtime-var-{name}"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "runtime-vars-cancel":
            self.dismiss(None)
            return
        self._dismiss_runtime_vars()

    def on_click(self, event: events.Click) -> None:
        widget_id = getattr(event.widget, "id", None)
        if widget_id == "runtime-vars-cancel":
            self.dismiss(None)
        elif widget_id == "runtime-vars-run":
            self._dismiss_runtime_vars()

    def _dismiss_runtime_vars(self) -> None:
        values: dict[str, str] = {}
        for item in self._variables:
            name = item["name"]
            values[name] = self.query_one(f"#runtime-var-{name}", Input).value
        self.dismiss(values)


class AnalyzeScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
    ]

    def __init__(self, pipeline_info: dict[str, Any]) -> None:
        super().__init__()
        self._pipeline_info = pipeline_info

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Container(
            Static(f"[bold]Analysis:[/] {self._pipeline_info['name']}", id="analysis-title"),
            LoadingIndicator(),
            Log(id="analysis-log"),
            Static(" Back ", id="back", classes="raw-log-action"),
            id="analysis-container",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._run_analysis()

    @work
    async def _run_analysis(self) -> None:
        log = self.query_one("#analysis-log", Log)
        loading = self.query_one(LoadingIndicator)
        loading.display = False

        path = Path(self._pipeline_info["path"])
        try:
            data = load_and_compile_pipeline(path)
            log.write(f"[green]v[/] Parsed pipeline: {path}")
        except Exception as e:
            log.write(f"[red]x[/] Failed to parse: {e}")
            return

        settings = _load_settings()
        result = analyze_pipeline(data, settings, Path(settings.task_cache_dir))

        log.write(f"\n[bold]Summary:[/] {result.summary()}")
        log.write(f"  Tasks: {result.task_count}")
        log.write(f"  Variables: {result.variable_count}")

        if result.missing_variables:
            log.write(f"\n[red]Missing Variables:[/]")
            for v in result.missing_variables:
                log.write(f"  x {v}")

        if result.missing_parameters:
            log.write(f"\n[yellow]Missing Parameters:[/]")
            for p in result.missing_parameters:
                log.write(f"  ! {p}")

        if result.missing_tasks:
            log.write(f"\n[red]Missing Tasks:[/]")
            for t in result.missing_tasks:
                log.write(f"  x {t}")

        if result.missing_service_connections:
            log.write(f"\n[yellow]Missing Service Connections:[/]")
            for c in result.missing_service_connections:
                log.write(f"  ! {c}")

        if not result.has_issues:
            log.write(f"\n[green bold]v No issues found -- ready to run![/]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.app.pop_screen()

    def on_click(self, event: events.Click) -> None:
        if getattr(event.widget, "id", None) == "back":
            self.app.pop_screen()

    def action_back(self) -> None:
        self.app.pop_screen()


class RunResultScreen(ModalScreen):
    STEP_NAME_WIDTH = 34
    ICON_PENDING = "○"
    RUNNING_ICONS = ("◐", "◓", "◑", "◒")
    ICON_SUCCEEDED = "✅"
    ICON_FAILED = "❌"
    ICON_SKIPPED = "-"
    DEFAULT_LOG_TAIL_LINES = 100
    LOG_PAGE_LINES = 500
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("c", "close", "Close"),
        Binding("y", "copy_logs", "Copy Logs"),
        Binding("m", "more_logs", "More Logs"),
        Binding("d", "close_and_delete", "Delete"),
        Binding("r", "restart_from_step", "Restart Step"),
    ]

    def __init__(self, pipeline: Pipeline, duration: float, preflight_logs: list[str] | None = None,
                 double_pop: bool = True, pipeline_info: dict[str, Any] | None = None,
                 run_timestamp: float | None = None) -> None:
        super().__init__()
        self._pipeline = pipeline
        self._duration = duration
        self._preflight_logs = preflight_logs or []
        self._all_log_lines: list[str] = []
        self._double_pop = double_pop
        self._pipeline_info = pipeline_info
        self._run_timestamp = run_timestamp
        self._selected_result_node: TreeNode | None = None
        self._visible_result_lines: dict[int, int] = {}
        self._node_status: dict[int, str] = {}
        self._restart_step_index: int | None = None
        self._artifact_paths: dict[str, str] = {}

    def action_copy_logs(self) -> None:
        text = "\n".join(self._selected_result_log_lines())
        try:
            _copy_text_to_clipboard(text)
            log = self.query_one("#result-log", RichLog)
            log.write(Text.from_markup("[dim]Selected raw log copied to clipboard[/]"))
        except Exception as e:
            log = self.query_one("#result-log", RichLog)
            log.write(Text.from_markup(f"[red]Copy failed: {e}[/]"))

    def compose(self) -> ComposeResult:
        yield Container(
            Static("", id="result-title"),
            Static("", id="result-duration"),
            Horizontal(id="result-artifacts"),
            Horizontal(
                Vertical(
                    Static("[bold]Steps[/]", id="result-steps-header"),
                    Tree("steps", id="result-tree"),
                    id="result-steps-panel",
                ),
                Vertical(
                    Horizontal(
                        Static("", id="result-logs-header"),
                        Static(" View raw log ", id="copy-result-logs", classes="raw-log-action"),
                        id="result-logs-toolbar",
                    ),
                    Static("", id="result-step-detail"),
                    RichLog(id="result-log", highlight=True, markup=True, wrap=True, max_lines=1000),
                    id="result-logs-panel",
                ),
                    id="result-panels",
                ),
                Horizontal(
                    Static(" Restart Step ", id="restart-step", classes="raw-log-action"),
                    Static(" Close ", id="close", classes="raw-log-action"),
                    id="result-footer",
                ),
                id="result-container",
            )

    def on_mount(self) -> None:
        tree = self.query_one("#result-tree", Tree)
        tree.show_root = False
        all_succeeded = True
        failed = 0
        succeeded = 0

        status_icon = self.ICON_SUCCEEDED
        status_color = "green"
        for _, job in _pipeline_jobs_with_stage(self._pipeline):
            for step in job.steps:
                if step.status == StepStatus.FAILED:
                    status_icon = self.ICON_FAILED
                    status_color = "red"
                    failed += 1
                    all_succeeded = False
                elif step.status == StepStatus.SUCCEEDED:
                    succeeded += 1
                elif step.status not in (StepStatus.SUCCEEDED, StepStatus.SKIPPED):
                    all_succeeded = False

        root_node = tree.root.add(Text.assemble((status_icon, status_color), " ", ("Pipeline", ""), (f" ({_format_duration(self._duration)})", "dim")))

        preflight_node = root_node.add_leaf(f"[green]{self.ICON_SUCCEEDED}[/] Initialize job")
        preflight_node.data = self._preflight_logs
        self._node_status[id(preflight_node)] = "completed"

        step_idx = 0
        for stage_name, job in _pipeline_jobs_with_stage(self._pipeline):
            parent = root_node
            if stage_name:
                parent = root_node.add(f"Stage: {stage_name}")
            job_node = parent.add(f"Job: {job.display_name or job.name}")
            for step in job.steps:
                icon = self.ICON_SUCCEEDED if step.status == StepStatus.SUCCEEDED else self.ICON_FAILED if step.status == StepStatus.FAILED else self.ICON_SKIPPED
                dur = _format_duration(step.duration) if step.duration else ""
                color = "green" if step.status == StepStatus.SUCCEEDED else "red" if step.status == StepStatus.FAILED else "dim"
                label = _step_display_name(step, type(step).__name__)
                row = self._format_step_row(str(label), dur)
                node = job_node.add_leaf(f"[{color}]{icon}[/] {row}")
                node.data = (step_idx, step.logs[:])
                self._node_status[id(node)] = step.status.value if isinstance(step.status, StepStatus) else str(step.status)
                step_idx += 1

        tree.root.expand_all()
        self._update_result_header(all_succeeded, succeeded, failed)
        self._render_artifacts()

        self._selected_result_node = preflight_node
        self._render_result_node(preflight_node)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        if event.node.data:
            self._selected_result_node = event.node
            self._render_result_node(event.node)

    def action_more_logs(self) -> None:
        if not self._selected_result_node or not self._selected_result_node.data:
            return
        key = id(self._selected_result_node)
        current = self._visible_result_lines.get(key, self.DEFAULT_LOG_TAIL_LINES)
        self._visible_result_lines[key] = current + self.LOG_PAGE_LINES
        self._render_result_node(self._selected_result_node)

    def _render_result_node(self, node: TreeNode) -> None:
        log = self.query_one("#result-log", RichLog)
        header = self.query_one("#result-logs-header", Static)
        detail = self.query_one("#result-step-detail", Static)
        log.clear()
        title = node.label.plain.strip()
        raw = node.data
        if isinstance(raw, tuple):
            step_idx, lines = raw
        else:
            step_idx, lines = None, (raw or [])
        visible = self._visible_result_lines.get(id(node), self.DEFAULT_LOG_TAIL_LINES)
        shown_lines = lines[-visible:] if len(lines) > visible else lines
        status = self._node_status.get(id(node), "completed")
        header.update(Text.from_markup(f"[bold]{title}[/]"))
        detail.update(Text.from_markup(
            f"[dim]Status: {status} | Showing {len(shown_lines)} of {len(lines)} lines | Full log retained for View raw log[/]"
        ))
        self._all_log_lines.clear()
        self._all_log_lines.append(title)
        if len(lines) > visible:
            log.write(Text.from_markup(f"[dim]Showing last {visible} of {len(lines)} lines. Press 'm' for more.[/]"))
        if not shown_lines:
            log.write(Text.from_markup("[dim]No log output captured.[/]"))
            return
        start_line = max(1, len(lines) - len(shown_lines) + 1)
        width = len(str(len(lines)))
        for offset, line in enumerate(shown_lines, start_line):
            prefix = f"{offset:>{width}} | "
            log.write(Text.assemble((prefix, "dim"), str(line)))
            self._all_log_lines.append(line)

    def _update_result_header(self, all_succeeded: bool, succeeded: int, failed: int) -> None:
        title = self.query_one("#result-title", Static)
        duration = self.query_one("#result-duration", Static)
        status = "succeeded" if all_succeeded else "failed"
        color = "green" if all_succeeded else "red"
        run_id = datetime.now().strftime("%Y%m%d.%H%M%S")
        title.update(Text.from_markup(f"[bold]Jobs in run #{run_id}[/]  [{color}]{status}[/]"))
        duration.update(Text.from_markup(
            f"[dim]{self._pipeline.name} | Job | elapsed {_format_duration(self._duration)} | {succeeded} succeeded | {failed} failed[/]"
        ))

    def _render_artifacts(self) -> None:
        container = self.query_one("#result-artifacts", Horizontal)
        self._artifact_paths.clear()
        artifacts: list[tuple[str, str]] = []
        for job in self._pipeline.jobs:
            for step in job.steps:
                if isinstance(step, PublishStep):
                    path = step.artifact_path or self._artifact_path_from_logs(step.logs)
                    if path:
                        name = Path(path).name or step.artifact or path
                        artifacts.append((name, path))
        if not artifacts:
            container.display = False
            return
        container.display = True
        for index, (name, path) in enumerate(artifacts):
            artifact_id = f"artifact-{index}"
            self._artifact_paths[artifact_id] = path
            container.mount(Static(f" 📦 {name} ", id=artifact_id, classes="artifact-link"))

    def _artifact_path_from_logs(self, logs: list[str]) -> str | None:
        for line in reversed(logs):
            if line.startswith("Published artifact:"):
                path = line.split(":", 1)[1].strip()
                return path or None
        return None

    def _collect_result_logs(self) -> list[str]:
        lines: list[str] = [f"Jobs in run: {self._pipeline.name}", f"Duration: {_format_duration(self._duration)}", ""]
        if self._preflight_logs:
            lines.append("Initialize job")
            lines.extend(self._preflight_logs)
            lines.append("")
        for job in self._pipeline.jobs:
            for step in job.steps:
                label = getattr(step, "display_name", None) or getattr(step, "task", type(step).__name__)
                lines.append(str(label))
                lines.extend(step.logs)
                lines.append("")
        return lines

    def _selected_result_log_lines(self) -> list[str]:
        if not self._selected_result_node or not self._selected_result_node.data:
            return self._collect_result_logs()
        title = self._selected_result_node.label.plain.strip()
        raw = self._selected_result_node.data
        if isinstance(raw, tuple):
            _, lines = raw
        else:
            lines = raw or []
        return [title, *[str(line) for line in lines]]

    def _format_step_row(self, label: str, duration: str = "") -> str:
        if len(label) > self.STEP_NAME_WIDTH:
            label = label[: self.STEP_NAME_WIDTH - 1] + "…"
        return f"{label:<{self.STEP_NAME_WIDTH}} {duration:>8}"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self._do_close()

    def on_click(self, event: events.Click) -> None:
        if getattr(event.widget, "id", None) == "copy-result-logs":
            self.action_copy_logs()
        elif getattr(event.widget, "id", None) == "close":
            self._do_close()
        elif getattr(event.widget, "id", None) == "restart-step":
            self.action_restart_from_step()
        elif getattr(event.widget, "id", None) in self._artifact_paths:
            _open_path_location(self._artifact_paths[getattr(event.widget, "id")])

    def action_close(self) -> None:
        self._do_close()

    def action_restart_from_step(self) -> None:
        if not self._selected_result_node or not self._selected_result_node.data:
            return
        raw = self._selected_result_node.data
        if not isinstance(raw, tuple):
            return
        step_idx, _ = raw
        if step_idx is None:
            return
        workspace_path = self._pipeline.workspace_dir
        if not workspace_path or not Path(workspace_path).is_dir():
            return
        path = self._pipeline_info["path"] if self._pipeline_info else None
        if not path or not Path(path).is_file():
            return
        pipeline_info = parse_pipeline_info(Path(path))
        pipeline_info["resolved_params"] = dict(self._pipeline.parameters)
        self.app.pop_screen()
        if self._double_pop:
            self.app.pop_screen()
        self.app.push_screen(RunScreen(pipeline_info, resume_step_index=step_idx,
                                        resume_workspace_path=workspace_path,
                                        resume_pipeline=self._pipeline))

    def action_close_and_delete(self) -> None:
        path = self._pipeline_info["path"] if self._pipeline_info else None
        if path:
            records = _list_run_records(path)
            for rec in records:
                if self._run_timestamp and abs(rec.timestamp - self._run_timestamp) < 1.0:
                    _delete_run_record(rec)
                    break
        self._do_close()

    def _do_close(self) -> None:
        self.app.pop_screen()
        if self._double_pop:
            self.app.pop_screen()


class PATDialog(ModalScreen):
    def __init__(self, org_hint: str | None = None) -> None:
        super().__init__()
        self._org_hint = org_hint or ""

    def compose(self) -> ComposeResult:
        yield Container(
            Label("Azure DevOps Authentication", id="pat-title"),
            Static("A custom task was not found.\nEnter your Azure DevOps PAT to download it.\n\nRequired scope: [bold]Extensions (Read)[/] ([italic]vso.extension[/])\nCreate one at: [underline]https://dev.azure.com/{org}/_usersSettings/tokens[/]"),
            Input(placeholder="Organization", value=self._org_hint, id="org-input"),
            Input(placeholder="Personal Access Token (PAT)", password=True, id="token-input"),
            Input(placeholder="Project (optional)", id="project-input"),
            Horizontal(
                Static(" Save & Download ", id="save-btn", classes="raw-log-action success-action"),
                Static(" Skip ", id="skip-btn", classes="raw-log-action danger-action"),
                id="pat-buttons",
            ),
            id="pat-container",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-btn":
            org = self.query_one("#org-input", Input).value.strip()
            token = self.query_one("#token-input", Input).value.strip()
            project = self.query_one("#project-input", Input).value.strip()
            if org and token:
                self.dismiss((org, token, project))
            else:
                self.query_one("#pat-title", Label).update("[red]Organization and Token are required[/]")
        else:
            self.dismiss(None)

    def on_click(self, event: events.Click) -> None:
        widget_id = getattr(event.widget, "id", None)
        if widget_id == "save-btn":
            org = self.query_one("#org-input", Input).value.strip()
            token = self.query_one("#token-input", Input).value.strip()
            project = self.query_one("#project-input", Input).value.strip()
            if org and token:
                self.dismiss((org, token, project))
            else:
                self.query_one("#pat-title", Label).update("[red]Organization and Token are required[/]")
        elif widget_id == "skip-btn":
            self.dismiss(None)


class RetryDialog(ModalScreen[bool]):
    def compose(self) -> ComposeResult:
        yield Container(
            Label("Some tasks could not be resolved.\nContinue anyway?"),
            Horizontal(
                Static(" Continue ", id="continue-btn", classes="raw-log-action success-action"),
                Static(" Cancel ", id="cancel-btn", classes="raw-log-action danger-action"),
                id="retry-buttons",
            ),
            id="retry-container",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "continue-btn":
            self.dismiss(True)
        else:
            self.dismiss(False)

    def on_click(self, event: events.Click) -> None:
        if getattr(event.widget, "id", None) == "continue-btn":
            self.dismiss(True)
        elif getattr(event.widget, "id", None) == "cancel-btn":
            self.dismiss(False)


class RunScreen(Screen):
    STEP_NAME_WIDTH = 34
    ICON_PENDING = "○"
    RUNNING_ICONS = ("◐", "◓", "◑", "◒")
    ICON_SUCCEEDED = "✅"
    ICON_FAILED = "❌"
    ICON_SKIPPED = "-"
    DEFAULT_LOG_TAIL_LINES = 100
    LOG_PAGE_LINES = 500
    BINDINGS = [
        Binding("s", "start", "Start"),
        Binding("c", "cancel", "Cancel"),
        Binding("escape", "cancel", "Cancel"),
        Binding("y", "copy_logs", "Copy Logs"),
        Binding("m", "more_logs", "More Logs"),
    ]

    def __init__(self, pipeline_info: dict[str, Any],
                 resume_step_index: int | None = None,
                 resume_workspace_path: str | None = None,
                 resume_pipeline: Pipeline | None = None) -> None:
        super().__init__()
        self._pipeline_info = pipeline_info
        self._pipeline: Optional[Pipeline] = None
        self._engine: Optional[PipelineEngine] = None
        self._start_time: float = 0.0
        self._active_step_index: int | None = None
        self._cancelled: bool = False
        self._preflight_node: TreeNode | None = None
        self._preflight_active: bool = False
        self._preflight_logs: list[str] = []
        self._preflight_continue_event: threading.Event | None = None
        self._preflight_continue_value: bool = True
        self._all_log_lines: list[str] = []
        self._selected_log_index: int | str | None = None
        self._visible_log_lines: dict[int | str, int] = {}
        self._step_status: dict[int, str] = {}
        self._step_durations: dict[int, float] = {}
        self._step_started_at: dict[int, float] = {}
        self._preflight_started_at: float | None = None
        self._preflight_duration: float | None = None
        self._spinner_index: int = 0
        self._last_log_render_at: float = 0.0
        self._resume_step_index: int | None = resume_step_index
        self._resume_workspace_path: str | None = resume_workspace_path
        self._resume_pipeline: Pipeline | None = resume_pipeline
        self._started: bool = False
        self._can_start: bool = True

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Container(
            Static("", id="run-title"),
            Static("", id="run-summary"),
            Horizontal(
                Vertical(
                    Static("[bold]Steps[/]", id="steps-header"),
                    Tree("steps", id="step-tree"),
                    id="steps-panel",
                ),
                Vertical(
                    Horizontal(
                        Static("", id="logs-header"),
                        Static(" View raw log ", id="copy-logs", classes="raw-log-action"),
                        id="logs-toolbar",
                    ),
                    Static("", id="step-detail"),
                    RichLog(id="step-log", highlight=True, markup=True, wrap=True, max_lines=1000),
                    id="logs-panel",
                ),
                id="run-panels",
            ),
            Horizontal(
                Static("", id="run-footer"),
                Static(" Start ", id="start", classes="raw-log-action success-action"),
                Static(" Cancel ", id="cancel", classes="raw-log-action danger-action"),
                id="run-footer-row",
            ),
            id="run-container",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._log_widget = self.query_one("#step-log", RichLog)
        self._build_step_tree()
        self.set_interval(0.15, self._tick_spinner)
        self._all_log_lines.append("Review the compiled pipeline plan, then press Start to run.")
        self._update_run_header("preview")
        self._render_selected_log()

    def _write_log(self, msg: str) -> None:
        try:
            text = Text.from_markup(msg)
        except Exception:
            text = Text(msg)
        self._all_log_lines.append(text.plain)
        if self._selected_log_index == "preflight":
            self.app.call_from_thread(self._render_selected_log_throttled)
        else:
            self.app.call_from_thread(self._log_widget.write, text)

    def _start_preflight(self) -> None:
        self._preflight_active = True
        self._preflight_started_at = time.time()
        self._preflight_duration = None
        self._selected_log_index = "preflight"
        self._update_preflight_node(self.RUNNING_ICONS[self._spinner_index], "yellow")
        self._all_log_lines.append(">> preparation")
        self._render_selected_log()

    def _finish_preflight(self, failed: bool = False) -> None:
        self._preflight_active = False
        if self._preflight_started_at is not None:
            self._preflight_duration = time.time() - self._preflight_started_at
        if failed:
            self._update_preflight_node(self.ICON_FAILED, "red")
            self._preflight_logs.append("preparation failed")
        else:
            self._update_preflight_node(self.ICON_SUCCEEDED, "green")
            self._preflight_logs.append("preparation complete")
        if self._selected_log_index == "preflight":
            self._render_selected_log()

    def _update_preflight_node(self, icon: str, style: str) -> None:
        if self._preflight_node:
            duration = self._preflight_duration
            if duration is None and self._preflight_started_at is not None and self._preflight_active:
                duration = time.time() - self._preflight_started_at
            duration_text = _format_duration(duration) if duration is not None else ""
            self._preflight_node.label = Text.assemble(
                (icon, style), " ", (self._format_step_row("Initialize job", duration_text), "")
            )

    def _build_step_tree(self) -> None:
        tree = self.query_one("#step-tree", Tree)
        tree.clear()
        tree.show_root = False
        params = self._pipeline_info.get("resolved_params", {})
        variable_overrides = self._pipeline_info.get("resolved_variables", {})
        self._root_node = tree.root
        self._step_nodes: list[TreeNode] = []
        self._step_data: list[Any] = []
        self._step_display_names: list[str] = []
        self._step_logs: dict[int, list[str]] = {}
        self._extra_node_logs: dict[int, list[str]] = {}
        self._extra_node_titles: dict[int, str] = {}

        path = Path(self._pipeline_info["path"])
        try:
            data = load_and_compile_pipeline(path, params, variable_overrides)
            preview_pipeline = parse_pipeline_model(data, path)
        except Exception as e:
            tree.root.add(f"[red]{self.ICON_FAILED} Failed to compile pipeline[/]")
            self._all_log_lines.append(f"Compile failed: {e}")
            if hasattr(self, "_log_widget"):
                self._log_widget.write(Text.from_markup(f"[red]Compile failed: {e}[/]"))
            self._can_start = False
            self.query_one("#start", Static).add_class("disabled-action")
            return

        input_preview = self._preview_inputs(preview_pipeline.variables, params)
        self._all_log_lines.extend(input_preview)
        input_node = tree.root.add_leaf(f"{self.ICON_PENDING} Parameters / Variables")
        self._extra_node_logs[id(input_node)] = input_preview
        self._extra_node_titles[id(input_node)] = "Parameters / Variables"
        self._selected_log_index = f"node:{id(input_node)}"

        step_idx = 0
        jobs = _pipeline_jobs_with_stage(preview_pipeline)
        stage_nodes: dict[str, TreeNode] = {}
        job_nodes: dict[int, TreeNode] = {}
        default_job_node: TreeNode | None = None

        def get_job_node(stage_name: str | None, job: Job) -> TreeNode:
            nonlocal default_job_node
            cached = job_nodes.get(id(job))
            if cached is not None:
                return cached
            label = job.display_name or job.name
            if stage_name:
                stage_node = stage_nodes.get(stage_name)
                if stage_node is None:
                    stage_node = tree.root.add(f"{self.ICON_PENDING} Stage: {stage_name}")
                    stage_nodes[stage_name] = stage_node
                job_node = stage_node.add(f"{self.ICON_PENDING} Job: {label}")
                job_nodes[id(job)] = job_node
                return job_node
            if default_job_node is None:
                default_job_node = tree.root.add(f"{self.ICON_PENDING} Job: {label}")
            job_nodes[id(job)] = default_job_node
            return default_job_node

        first_job_node = get_job_node(jobs[0][0], jobs[0][1]) if jobs else tree.root.add(f"{self.ICON_PENDING} Job")

        preflight_node = first_job_node.add_leaf(Text.assemble((self.ICON_PENDING, "dim"), " ", (self._format_step_row("Initialize job"), "")))
        self._preflight_node = preflight_node
        self._preflight_logs = []

        for stage_name, job in jobs:
            job_node = get_job_node(stage_name, job)
            for step in job.steps:
                display = _step_display_name(step, f"step {step_idx}")
                previous = self._previous_step(step_idx) if self._resume_step_index is not None and step_idx < self._resume_step_index else None
                if previous is not None:
                    status = previous.status.value if isinstance(previous.status, StepStatus) else str(previous.status)
                    icon = self.ICON_SUCCEEDED if previous.status == StepStatus.SUCCEEDED else self.ICON_FAILED if previous.status == StepStatus.FAILED else self.ICON_SKIPPED
                    style = "green" if previous.status == StepStatus.SUCCEEDED else "red" if previous.status == StepStatus.FAILED else "dim"
                    duration = _format_duration(previous.duration) if previous.duration else ""
                else:
                    status = "pending"
                    icon = self.ICON_PENDING
                    style = "dim"
                    duration = ""
                node = job_node.add_leaf(Text.assemble((icon, style), " ", (self._format_step_row(str(display), duration), "")))
                self._step_nodes.append(node)
                self._step_data.append(step)
                self._step_display_names.append(display)
                self._step_status[step_idx] = status
                if previous is not None:
                    self._step_logs[step_idx] = previous.logs[:]
                    if previous.duration is not None:
                        self._step_durations[step_idx] = previous.duration
                step_idx += 1

        tree.root.expand()
        for node in stage_nodes.values():
            node.expand_all()
        if default_job_node:
            default_job_node.expand_all()
        first_job_node.expand_all()

    def _handle_pipeline_event(self, event: PipelineEvent) -> None:
        self.app.call_from_thread(self._on_pipeline_event, event)

    def _cancel_requested(self) -> bool:
        return self._cancelled

    def action_copy_logs(self) -> None:
        text = "\n".join(self._selected_live_log_lines())
        try:
            _copy_text_to_clipboard(text)
            self._log_widget.write(Text.from_markup("[dim]Selected raw log copied to clipboard[/]"))
        except Exception as e:
            self._log_widget.write(Text.from_markup(f"[red]Copy failed: {e}[/]"))

    def action_cancel(self) -> None:
        if not self._started:
            self.app.pop_screen()
            return
        self._cancelled = True
        self.query_one("#cancel", Static).add_class("disabled-action")
        log = self.query_one("#step-log", RichLog)
        log.write(Text.from_markup("[red bold]Cancelling current step...[/]"))

    def action_start(self) -> None:
        if self._started:
            return
        if not self._can_start:
            return
        self._started = True
        self._start_time = time.time()
        self.query_one("#start", Static).add_class("disabled-action")
        self.query_one("#start", Static).update(" Started ")
        if self._resume_step_index is not None:
            self._update_run_header("resuming")
        else:
            self._update_run_header("preparing")
        self._start_preflight()
        self._run_pipeline()

    def action_more_logs(self) -> None:
        key: int | str | None = self._selected_log_index
        if key is None:
            key = "preflight" if self._preflight_node else None
        if key is None:
            return
        current = self._visible_log_lines.get(key, self.DEFAULT_LOG_TAIL_LINES)
        self._visible_log_lines[key] = current + self.LOG_PAGE_LINES
        if key == "preflight" and self._preflight_node:
            self._on_tree_node_clicked(self._preflight_node)
        elif isinstance(key, int) and 0 <= key < len(self._step_nodes):
            self._on_tree_node_clicked(self._step_nodes[key], key)

    def _show_retry_dialog(self) -> None:
        def on_result(proceed: bool) -> None:
            self._preflight_continue_value = proceed
            self._preflight_continue_event.set()

        self.app.push_screen(RetryDialog(), on_result)

    def _show_pat_dialog(self, event: threading.Event, result: list) -> None:
        from ado_local.execution.checkout import detect_git_remote, parse_azure_devops_remote
        org_hint = None
        remote = detect_git_remote()
        if remote:
            org, _ = parse_azure_devops_remote(remote)
            org_hint = org

        def on_result(data: tuple[str, str, str] | None) -> None:
            result[0] = data
            event.set()

        self.app.push_screen(PATDialog(org_hint=org_hint), on_result)

    def _show_runtime_variables_dialog(self, variables: list[dict[str, str]], event: threading.Event, result: list) -> None:
        def on_result(data: dict[str, str] | None) -> None:
            result[0] = data
            event.set()

        self.app.push_screen(RuntimeVariablesDialog(variables), on_result)

    def _prompt_runtime_variables(self, pipe_vars: dict[str, Any]) -> bool:
        runtime_vars = self._collect_runtime_variables(pipe_vars)
        if not runtime_vars:
            return True

        self._preflight_logs.append("Runtime variables:")
        self._write_log("[bold cyan]>> runtime variables[/]")
        for item in runtime_vars:
            self._preflight_logs.append(f"  {item['name']}: {item['expression']} -> {item['value']}")
            self._write_log(f"[dim]  {item['name']}: {item['expression']} -> {item['value']}[/]")

        event = threading.Event()
        result: list[dict[str, str] | None] = [None]
        self.app.call_from_thread(self._show_runtime_variables_dialog, runtime_vars, event, result)
        event.wait()
        overrides = result[0]
        if overrides is None:
            return False
        pipe_vars.update(overrides)
        for name, value in overrides.items():
            self._preflight_logs.append(f"  using {name}: {value}")
            self._write_log(f"[green]  using {name}: {value}[/]")
        return True

    def _collect_runtime_variables(self, pipe_vars: dict[str, Any]) -> list[dict[str, str]]:
        variables = dict(pipe_vars)
        variables.update(self._pipeline_info.get("resolved_variables", {}))
        context = {"variables": variables, "parameters": self._pipeline_info.get("resolved_params", {})}
        counters: dict[str, int] = {}
        result: list[dict[str, str]] = []
        for name, value in list(pipe_vars.items()):
            if isinstance(value, str) and "$[" in value:
                evaluated = eval_runtime_expression(value, context, counters)
                context["variables"][name] = evaluated
                result.append({"name": name, "expression": value, "value": evaluated})
        return result

    def _preview_inputs(self, pipe_vars: dict[str, Any], params: dict[str, Any]) -> list[str]:
        variables = dict(pipe_vars)
        variables.update(self._pipeline_info.get("resolved_variables", {}))
        context = {"variables": variables, "parameters": params}
        counters: dict[str, int] = {}
        lines = ["Parameter preview:"]
        if params:
            for name, value in params.items():
                lines.append(f"  {name}: {value}")
        else:
            lines.append("  (none)")
        lines.append("")
        lines.append("Variable preview:")
        for name, value in list(variables.items()):
            display = value
            if isinstance(value, str) and "$[" in value:
                display = eval_runtime_expression(value, context, counters)
                context["variables"][name] = display
            lines.append(f"  {name}: {display}")
        return lines

    def _save_settings(self, settings: LocalSettings) -> None:
        try:
            path = Path(settings.workspace_root) / settings.settings_file
            data = {}
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
            data["azure_devops_org"] = settings.azure_devops_org
            data["azure_devops_token"] = settings.azure_devops_token
            data["azure_devops_project"] = settings.azure_devops_project
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _prompt_for_pat(self, settings: LocalSettings) -> None:
        pat_event = threading.Event()
        pat_result: list[tuple[str, str, str] | None] = [None]
        self.app.call_from_thread(self._show_pat_dialog, pat_event, pat_result)
        pat_event.wait()
        pat_data = pat_result[0]
        if pat_data:
            org, token, project = pat_data
            settings.azure_devops_org = org
            settings.azure_devops_token = token
            settings.azure_devops_project = project or settings.azure_devops_project
            self._save_settings(settings)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start":
            self.action_start()
        elif event.button.id == "cancel":
            self.action_cancel()

    def on_click(self, event: events.Click) -> None:
        if getattr(event.widget, "id", None) == "copy-logs":
            self.action_copy_logs()
        elif getattr(event.widget, "id", None) == "start":
            self.action_start()
        elif getattr(event.widget, "id", None) == "cancel":
            self.action_cancel()

    def _tick_spinner(self) -> None:
        if self._preflight_active:
            self._spinner_index = (self._spinner_index + 1) % len(self.RUNNING_ICONS)
            self._update_preflight_node(self.RUNNING_ICONS[self._spinner_index], "yellow")
            self._update_run_header("preparing")
        elif self._active_step_index is not None:
            self._spinner_index = (self._spinner_index + 1) % len(self.RUNNING_ICONS)
            self._update_step_node(self._active_step_index, self.RUNNING_ICONS[self._spinner_index], "yellow")
            self._update_run_header("running")

    def _on_pipeline_event(self, event: PipelineEvent) -> None:
        if event.event_type == EventType.STEP_START:
            self._active_step_index = event.step_index or 0
            self._selected_log_index = self._active_step_index
            self._step_status[self._active_step_index] = "running"
            self._step_started_at[self._active_step_index] = time.time()
            self._spinner_index = 0
            self._update_step_node(self._active_step_index, self.RUNNING_ICONS[self._spinner_index], "yellow")
            self._all_log_lines.append(f">> {event.step_name or ''}")
            self._render_selected_log()

        elif event.event_type == EventType.STEP_LOG:
            line = str(event.log_line or "")
            self._all_log_lines.append(line)
            idx = event.step_index or 0
            self._step_logs.setdefault(idx, []).append(line)
            if self._selected_log_index == idx:
                self._render_selected_log_throttled()

        elif event.event_type == EventType.STEP_COMPLETE:
            idx = event.step_index or 0
            self._active_step_index = None
            started_at = self._step_started_at.get(idx)
            if event.duration:
                self._step_durations[idx] = event.duration
            elif started_at is not None:
                self._step_durations[idx] = time.time() - started_at
            self._step_started_at.pop(idx, None)
            if event.status == "succeeded" or event.status == StepStatus.SUCCEEDED.value:
                self._step_status[idx] = "succeeded"
                self._update_step_node(idx, self.ICON_SUCCEEDED, "green")
            elif event.status == "failed" or event.status == StepStatus.FAILED.value:
                self._step_status[idx] = "failed"
                self._update_step_node(idx, self.ICON_FAILED, "red")
            else:
                self._step_status[idx] = "skipped"
                self._update_step_node(idx, self.ICON_SKIPPED, "dim")
            if self._selected_log_index == idx:
                self._render_selected_log()

        elif event.event_type == EventType.ERROR:
            self._all_log_lines.append(f"ERROR: {event.message}")
            if self._active_step_index is not None:
                self._step_logs.setdefault(self._active_step_index, []).append(f"ERROR: {event.message}")
            if self._selected_log_index is not None:
                self._render_selected_log()

        elif event.event_type == EventType.WARNING:
            self._all_log_lines.append(f"WARNING: {event.message}")
            if self._active_step_index is not None:
                self._step_logs.setdefault(self._active_step_index, []).append(f"WARNING: {event.message}")
            if self._selected_log_index is not None:
                self._render_selected_log()

        elif event.event_type == EventType.PIPELINE_COMPLETE:
            duration = time.time() - self._start_time
            footer = self.query_one("#run-footer", Static)
            if self._cancelled:
                footer.update(Text.from_markup(f"[red bold]Pipeline cancelled after {_format_duration(duration)}[/]"))
                self._update_run_header("cancelled")
            else:
                footer.update(Text.from_markup(f"[green bold]Pipeline complete in {_format_duration(duration)}[/]"))
                self._update_run_header("completed")
            _save_run_record(self._pipeline_info, self._pipeline, duration, self._preflight_logs)
            self.app.push_screen(RunResultScreen(self._pipeline, duration, self._preflight_logs,
                                                   pipeline_info=self._pipeline_info,
                                                   run_timestamp=time.time()))

        if event.event_type != EventType.PIPELINE_COMPLETE:
            self._update_run_header("running")

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        if event.node is self._preflight_node:
            self._on_tree_node_clicked(event.node)
            return
        if id(event.node) in getattr(self, "_extra_node_logs", {}):
            self._selected_log_index = f"node:{id(event.node)}"
            self._render_selected_log()
            return
        try:
            idx = self._step_nodes.index(event.node)
        except ValueError:
            return
        self._on_tree_node_clicked(event.node, idx)

    def _update_step_node(self, index: int, icon: str, style: str, suffix: str = "") -> None:
        if 0 <= index < len(self._step_nodes):
            node = self._step_nodes[index]
            display_name = self._step_display_names[index] if index < len(self._step_display_names) else str(index)
            duration = self._step_durations.get(index)
            if duration is None and index in self._step_started_at:
                duration = time.time() - self._step_started_at[index]
            duration_text = _format_duration(duration) if duration is not None else suffix
            node.label = Text.assemble((icon, style), " ", (self._format_step_row(str(display_name), duration_text), ""))

    def _update_job_node(self) -> None:
        if not hasattr(self, "_root_node"):
            return
        tree = self.query_one("#step-tree", Tree)
        if not tree.root.children:
            return
        job_node = tree.root.children[0]
        duration = time.time() - self._start_time if self._start_time else 0.0
        failed = any(value == "failed" for value in self._step_status.values())
        running = self._active_step_index is not None or self._preflight_active
        if failed:
            icon, style = self.ICON_FAILED, "red"
        elif running:
            icon, style = self.RUNNING_ICONS[self._spinner_index], "yellow"
        else:
            icon, style = self.ICON_SUCCEEDED, "green"
        job_node.label = Text.assemble((icon, style), " ", ("Job", ""), (f" ({_format_duration(duration)})", "dim"))

    def _format_step_row(self, label: str, duration: str = "") -> str:
        if len(label) > self.STEP_NAME_WIDTH:
            label = label[: self.STEP_NAME_WIDTH - 1] + "…"
        return f"{label:<{self.STEP_NAME_WIDTH}} {duration:>8}"

    def _previous_step(self, index: int) -> Any | None:
        if not self._resume_pipeline or not self._resume_pipeline.jobs:
            return None
        steps = self._resume_pipeline.jobs[0].steps
        if 0 <= index < len(steps):
            return steps[index]
        return None

    def _copy_previous_step_state(self, index: int, step: Any) -> Any:
        previous = self._previous_step(index)
        if previous is None:
            return step
        for attr in ("status", "logs", "start_time", "end_time", "exit_code", "artifact_path"):
            if hasattr(step, attr) and hasattr(previous, attr):
                value = getattr(previous, attr)
                setattr(step, attr, value[:] if isinstance(value, list) else value)
        return step

    def _selected_live_log_lines(self) -> list[str]:
        key = self._selected_log_index
        if key == "preflight":
            return ["Initialize job", *self._preflight_logs]
        if isinstance(key, str) and key.startswith("node:"):
            try:
                node_id = int(key.split(":", 1)[1])
            except ValueError:
                return []
            return self._extra_node_logs.get(node_id, [])
        if isinstance(key, int) and 0 <= key < len(self._step_display_names):
            return [str(self._step_display_names[key]), *self._step_logs.get(key, [])]
        return self._all_log_lines[:]

    def _on_tree_node_clicked(self, node: TreeNode, idx: int | None = None) -> None:
        if node is self._preflight_node:
            self._selected_log_index = "preflight"
        elif idx is not None and 0 <= idx < len(self._step_display_names):
            self._selected_log_index = idx
        self._render_selected_log()

    def _update_run_header(self, status: str) -> None:
        elapsed = time.time() - self._start_time if self._start_time else 0.0
        total = len(self._step_nodes) + (1 if self._preflight_node else 0)
        succeeded = sum(1 for value in self._step_status.values() if value == "succeeded")
        failed = sum(1 for value in self._step_status.values() if value == "failed")
        running = 1 if self._active_step_index is not None or self._preflight_active else 0
        title = self.query_one("#run-title", Static)
        summary = self.query_one("#run-summary", Static)
        footer = self.query_one("#run-footer", Static)
        color = {
            "preview": "cyan",
            "completed": "green",
            "cancelled": "red",
            "running": "yellow",
            "preparing": "cyan",
            "resuming": "cyan",
        }.get(status, "white")
        run_id = datetime.fromtimestamp(self._start_time or time.time()).strftime("%Y%m%d.%H%M%S")
        title_prefix = f"Jobs in run #{run_id}" if self._started else "Pipeline preview"
        title.update(Text.from_markup(f"[bold]{title_prefix}[/]  [{color}]{status}[/]"))
        summary.update(Text.from_markup(
            f"[dim]{self._pipeline_info['name']} | elapsed {_format_duration(elapsed)} | {succeeded} succeeded | {failed} failed | {running} running | {total} total[/]"
        ))
        if self._started:
            footer.update(Text.from_markup("[dim]Use arrows/click to select a step, 'm' for more log lines, 'y' or Copy Logs for selected raw log.[/]"))
            self._update_job_node()
        else:
            footer.update(Text.from_markup("[bold cyan]Review the compiled plan, then press Start or 's' to run.[/]"))

    def _render_selected_log(self) -> None:
        log = self.query_one("#step-log", RichLog)
        header = self.query_one("#logs-header", Static)
        detail = self.query_one("#step-detail", Static)
        log.clear()
        key = self._selected_log_index
        if key == "preflight":
            title = "Initialize job"
            status = "running" if self._preflight_active else "completed"
            lines = self._preflight_logs
        elif isinstance(key, str) and key.startswith("node:"):
            try:
                node_id = int(key.split(":", 1)[1])
            except ValueError:
                node_id = 0
            title = self._extra_node_titles.get(node_id, "Preview")
            status = "preview"
            lines = self._extra_node_logs.get(node_id, [])
        elif isinstance(key, int) and 0 <= key < len(self._step_display_names):
            title = self._step_display_names[key]
            status = self._step_status.get(key, "pending")
            lines = self._step_logs.get(key, [])
        else:
            title = "Logs"
            status = "pending"
            lines = []

        visible = self._visible_log_lines.get(key, self.DEFAULT_LOG_TAIL_LINES) if key is not None else self.DEFAULT_LOG_TAIL_LINES
        shown_lines = lines[-visible:] if len(lines) > visible else lines
        header.update(Text.from_markup(f"[bold]{title}[/]"))
        detail.update(Text.from_markup(
            f"[dim]Status: {status} | Showing {len(shown_lines)} of {len(lines)} lines | Full log retained for Copy Logs[/]"
        ))
        if len(lines) > visible:
            log.write(Text.from_markup(f"[dim]Showing last {visible} of {len(lines)} lines. Press 'm' for more.[/]"))
        if not shown_lines:
            log.write(Text.from_markup("[dim]No log output yet.[/]"))
            return
        start_line = max(1, len(lines) - len(shown_lines) + 1)
        width = len(str(len(lines)))
        for offset, line in enumerate(shown_lines, start_line):
            prefix = f"{offset:>{width}} | "
            log.write(Text.assemble((prefix, "dim"), str(line)))

    def _render_selected_log_throttled(self) -> None:
        now = time.time()
        if now - self._last_log_render_at < 0.25:
            return
        self._last_log_render_at = now
        self._render_selected_log()

    @work(thread=True)
    def _run_pipeline(self) -> None:
        try:
            path = Path(self._pipeline_info["path"])

            params = self._pipeline_info.get("resolved_params", {})
            variable_overrides = self._pipeline_info.get("resolved_variables", {})
            param_defs = self._pipeline_info.get("parameters", [])
            for p in param_defs:
                if p["name"] not in params and p.get("default") is not None:
                    params[p["name"]] = p["default"]
            if self._resume_pipeline and self._resume_pipeline.parameters:
                params.update(self._resume_pipeline.parameters)
                self._pipeline_info["resolved_params"] = params

            data = load_and_compile_pipeline(path, params, variable_overrides)
            params = data.get("parameters", params) if isinstance(data.get("parameters"), dict) else params
            self._pipeline_info["resolved_params"] = params

            if self._resume_step_index is None:
                task_specs = collect_pipeline_tasks(data)
                if task_specs:
                    from ado_local.models.config import LocalSettings
                    from ado_local.cache.task_cache import resolve_task
                    from ado_local.execution.checkout import detect_git_remote, parse_azure_devops_remote
                    settings_ = _load_settings()
                    cache_dir = Path(settings_.task_cache_dir)
                    all_found = True
                    for spec in task_specs:
                        msg = f"[dim]  {spec}...[/]"
                        self._preflight_logs.append(f"  {spec}...")
                        self._write_log(msg)
                        result = resolve_task(
                            spec, cache_dir, auto_download=True,
                            log_callback=lambda m: self._preflight_logs.append(
                                m.removeprefix("  ") if m.startswith("  ") else m
                            ) or self._write_log(f"[dim]{m}[/]"),
                        )
                        if result is None:
                            if not settings_.azure_devops_token or not settings_.azure_devops_org:
                                self._prompt_for_pat(settings_)
                            ado_retries = 0
                            while ado_retries < 2 and settings_.azure_devops_token and settings_.azure_devops_org:
                                self._preflight_logs.append(f"  Retrying via Azure DevOps...")
                                self._write_log("[dim]  Retrying via Azure DevOps...[/]")
                                result = resolve_task(
                                    spec, cache_dir, auto_download=False,
                                    azure_devops_token=settings_.azure_devops_token,
                                    azure_devops_org=settings_.azure_devops_org,
                                    azure_devops_project=settings_.azure_devops_project,
                                    log_callback=lambda m: self._preflight_logs.append(
                                        m.removeprefix("  ") if m.startswith("  ") else m
                                    ) or self._write_log(f"[dim]{m}[/]"),
                                )
                                if result is not None:
                                    break
                                ado_retries += 1
                                if ado_retries < 2:
                                    self._preflight_logs.append(f"  ADO download failed — you may need to update your PAT")
                                    self._write_log("[yellow]  ADO download failed — you may need to update your PAT[/]")
                                    self._prompt_for_pat(settings_)
                        if result is None:
                            self._preflight_logs.append(f"  {spec} NOT FOUND")
                            self._write_log(f"[yellow]  {spec} NOT FOUND (will fail)[/]")
                            all_found = False
                        else:
                            line = f"  {spec} -> {result.name} {result.resolved_version}"
                            self._preflight_logs.append(line)
                            self._write_log(f"[green]{line}[/]")
                    self.app.call_from_thread(self._finish_preflight, not all_found)
                    if not all_found:
                        self._preflight_continue_event = threading.Event()
                        self._preflight_continue_value = False
                        self.app.call_from_thread(self._show_retry_dialog)
                        self._preflight_continue_event.wait()
                        if not self._preflight_continue_value:
                            cancelled_pipeline = Pipeline(name=self._pipeline_info["name"])
                            cancelled_pipeline.jobs = [Job(name="default")]
                            self.app.call_from_thread(
                                self.app.push_screen, RunResultScreen(cancelled_pipeline, 0.0, self._preflight_logs)
                            )
                            return
                else:
                    self.app.call_from_thread(self._finish_preflight, False)
            else:
                self.app.call_from_thread(self._finish_preflight, False)

            pipeline = parse_pipeline_model(data, path)
            pipe_vars = dict(pipeline.variables)
            pipe_vars.update(variable_overrides)

            if self._resume_pipeline:
                pipe_vars = dict(self._resume_pipeline.variables)
            elif not self._prompt_runtime_variables(pipe_vars):
                cancelled_pipeline = Pipeline(name=self._pipeline_info["name"])
                cancelled_pipeline.jobs = [Job(name="default")]
                self.app.call_from_thread(
                    self.app.push_screen, RunResultScreen(cancelled_pipeline, 0.0, self._preflight_logs)
                )
                return

            pipeline.variables = pipe_vars
            pipeline.parameters = params
            if self._resume_step_index is not None:
                for step_idx, step in enumerate(_pipeline_steps_flat(pipeline)):
                    if step_idx < self._resume_step_index:
                        previous = self._copy_previous_step_state(step_idx, step)
                        _replace_pipeline_step(pipeline, step, previous)
            self._pipeline = pipeline

            settings = _load_settings()
            if self._resume_step_index is not None and self._resume_workspace_path:
                from ado_local.execution.workspace import WorkspaceManager
                ws = WorkspaceManager.from_existing(settings, self._resume_workspace_path)
            else:
                ws = None
            self._engine = PipelineEngine(
                settings,
                event_handler=self._handle_pipeline_event,
                cancel_requested=self._cancel_requested,
            )
            self._engine.execute(pipeline,
                                 params=params,
                                 start_from_step=self._resume_step_index or 0,
                                 workspace=ws)

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            self.app.call_from_thread(self._log_widget.write, Text.from_markup(f"[red bold]Pipeline failed: {e}[/]"))
            self.app.call_from_thread(self._log_widget.write, Text.from_markup(f"[dim]{tb}[/]"))
            self.app.call_from_thread(self._finish_preflight, True)


class ConfirmQuitDialog(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        yield Container(
            Label("A pipeline is still running.\nQuit anyway?"),
            Horizontal(
                Static(" Yes ", id="yes", classes="raw-log-action danger-action"),
                Static(" No ", id="no", classes="raw-log-action"),
                id="confirm-buttons",
            ),
            id="confirm-container",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_click(self, event: events.Click) -> None:
        if getattr(event.widget, "id", None) == "yes":
            self.dismiss(True)
        elif getattr(event.widget, "id", None) == "no":
            self.dismiss(False)


class AdoLocalApp(App):
    SCREENS = {
        "select": PipelineSelectScreen,
    }
    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
    ]
    TITLE = "ado-local"
    SUB_TITLE = f"v{__version__}"
    CSS = """
    Screen {
        background: #1e1e1e;
    }
    #main-container {
        padding: 1;
    }
    #title {
        text-style: bold;
        color: #0078d4;
        content-align: center top;
        height: 3;
    }
    #version {
        color: #666;
        content-align: center top;
        height: 1;
    }
    #prompt {
        margin: 1 0;
    }
    #pipeline-list {
        height: 60%;
        border: solid #333;
        margin: 0 0 1 0;
    }
    #pipeline-info {
        height: 20%;
        border: solid #333;
        padding: 1;
        margin: 0 0 1 0;
    }
    #buttons {
        height: 1;
        align: center middle;
    }
    #analyze {
        width: 11;
        min-width: 11;
        margin: 0 1;
    }
    #run {
        width: 8;
        min-width: 8;
        margin: 0 1;
    }
    #refresh {
        width: 11;
        min-width: 11;
        margin: 0 1;
    }
    #quit {
        width: 9;
        min-width: 9;
        margin: 0 1;
    }
    Button {
        margin: 0 1;
    }
    #run-container {
        padding: 1;
    }
    #run-title {
        height: 1;
        margin: 0 0 0 0;
    }
    #run-summary {
        height: 1;
        margin: 0 0 1 0;
    }
    #run-panels {
        height: 1fr;
    }
    #steps-panel {
        width: 54;
        border: solid #333;
        padding: 0 1;
    }
    #logs-panel {
        width: 1fr;
        border: solid #333;
        padding: 0 1;
    }
    #steps-header {
        text-style: bold;
        height: 1;
        margin: 0 0 1 0;
    }
    #logs-header {
        text-style: bold;
        width: 1fr;
        height: 1;
        content-align: left middle;
    }
    #logs-toolbar {
        height: 1;
        align: right middle;
    }
    #copy-logs {
        width: 15;
        min-width: 15;
        height: 1;
        min-height: 1;
    }
    #cancel {
        width: 10;
        min-width: 10;
        height: 1;
        min-height: 1;
        margin: 0 1;
    }
    .raw-log-action {
        background: #2d2d2d;
        color: #ffffff;
        text-style: bold;
        content-align: center middle;
        height: 1;
        min-height: 1;
    }
    .raw-log-action:hover {
        background: #0078d4;
    }
    .danger-action {
        background: #5a1f1f;
    }
    .danger-action:hover {
        background: #a4262c;
    }
    .success-action {
        background: #107c41;
    }
    .success-action:hover {
        background: #13a10e;
    }
    .disabled-action {
        background: #333333;
        color: #777777;
    }
    #step-detail {
        height: 1;
        margin: 0 0 1 0;
    }
    #step-log {
        height: 1fr;
    }
    #run-footer {
        height: 1;
        color: #666;
        margin: 1 0 0 0;
    }
    #run-footer-row {
        height: 1;
        align: center middle;
    }
    #param-container {
        padding: 1;
    }
    #param-fields {
        height: 80%;
        border: solid #333;
        padding: 1;
        margin: 1 0;
    }
    #param-fields Input {
        margin: 0 0 1 0;
    }
    #param-buttons {
        height: 1;
        align: center middle;
    }
    #run-params {
        width: 8;
        min-width: 8;
        margin: 0 1;
    }
    #runtime-vars-buttons {
        height: 1;
        align: center middle;
    }
    #runtime-vars-run {
        width: 12;
        min-width: 12;
        margin: 0 1;
    }
    #runtime-vars-cancel {
        width: 10;
        min-width: 10;
        margin: 0 1;
    }
    #pat-buttons, #retry-buttons, #confirm-buttons {
        height: 1;
        align: center middle;
        margin: 1 0 0 0;
    }
    #save-btn {
        width: 19;
        min-width: 19;
        margin: 0 1;
    }
    #skip-btn {
        width: 9;
        min-width: 9;
        margin: 0 1;
    }
    #continue-btn {
        width: 12;
        min-width: 12;
        margin: 0 1;
    }
    #cancel-btn {
        width: 10;
        min-width: 10;
        margin: 0 1;
    }
    #yes {
        width: 8;
        min-width: 8;
        margin: 0 1;
    }
    #no {
        width: 7;
        min-width: 7;
        margin: 0 1;
    }
    #runtime-vars-dialog {
        width: 70%;
        height: 60%;
        padding: 1 2;
        border: solid #555;
        background: #1e1e1e;
    }
    #runtime-vars-fields {
        height: 1fr;
        margin: 1 0;
    }
    #runtime-vars-fields Input {
        margin: 0 0 1 0;
    }
    #analysis-container {
        padding: 1;
    }
    #analysis-log {
        height: 80%;
        border: solid #333;
        margin: 1 0;
    }
    #result-container {
        padding: 1;
    }
    #result-title {
        height: 1;
        margin: 0 0 0 0;
    }
    #result-duration {
        height: 1;
        margin: 0 0 1 0;
    }
    #result-artifacts {
        height: 1;
        margin: 0 0 1 0;
    }
    .artifact-link {
        background: #2d2d2d;
        color: #ffffff;
        text-style: bold;
        content-align: center middle;
        height: 1;
        margin: 0 1 0 0;
    }
    .artifact-link:hover {
        background: #0078d4;
    }
    #result-panels {
        height: 1fr;
    }
    #result-steps-panel {
        width: 54;
        border: solid #333;
        padding: 0 1;
    }
    #result-logs-panel {
        width: 1fr;
        border: solid #333;
        padding: 0 1;
    }
    #result-steps-header {
        text-style: bold;
        height: 1;
        margin: 0 0 1 0;
    }
    #result-logs-header {
        text-style: bold;
        width: 1fr;
        height: 1;
        content-align: left middle;
    }
    #result-logs-toolbar {
        height: 1;
        align: right middle;
    }
    #copy-result-logs {
        width: 15;
        min-width: 15;
        height: 1;
        min-height: 1;
    }
    #close {
        width: 10;
        min-width: 10;
        height: 1;
        min-height: 1;
        margin: 0 1;
    }
    #restart-step {
        width: 18;
        min-width: 18;
        height: 1;
        min-height: 1;
        margin: 0 1;
    }
    #result-footer {
        height: 1;
        align: center middle;
    }
    #result-step-detail {
        height: 1;
        margin: 0 0 1 0;
    }
    #result-log {
        height: 1fr;
    }
    #result-tree {
        height: 100%;
    }
    #retry-container {
        width: 50;
        height: 9;
        border: solid $secondary;
        background: $surface;
        padding: 1;
        align: center middle;
    }
    #history-container {
        padding: 1;
    }
    #history-title {
        text-style: bold;
        height: 1;
        margin: 0 0 1 0;
    }
    #new-run {
        width: 12;
        min-width: 12;
        height: 1;
        min-height: 1;
        margin: 0 1 0 0;
    }
    #open-artifact {
        width: 17;
        min-width: 17;
        height: 1;
        min-height: 1;
        margin: 0 0 0 0;
    }
    #history-actions {
        height: 1;
        margin: 0 0 1 0;
    }
    #back {
        width: 10;
        min-width: 10;
        height: 1;
        min-height: 1;
        margin: 1 0 0 0;
    }
    #history-table {
        height: 1fr;
        border: solid #333;
        margin: 1 0;
    }
    #history-table > .datatable--cursor {
        background: #303030;
        color: #ffffff;
    }
    #history-table > .datatable--hover {
        background: #252525;
    }
    #confirm-container {
        width: 50;
        height: 9;
        border: solid $secondary;
        background: $surface;
        padding: 1;
        align: center middle;
    }
    """

    def action_quit(self) -> None:
        if isinstance(self.screen, RunScreen) and not self.screen._cancelled:
            def _on_confirm(confirmed: bool | None) -> None:
                if confirmed:
                    self.exit()
            self.push_screen(ConfirmQuitDialog(), _on_confirm)
        else:
            self.exit()

    def on_mount(self) -> None:
        _cleanup_orphan_workspaces_async()
        self.push_screen(PipelineSelectScreen())
