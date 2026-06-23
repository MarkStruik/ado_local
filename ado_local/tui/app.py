from __future__ import annotations

import asyncio
import json
import os
import re
import time
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

from ado_local import __version__
from ado_local.analysis.analyzer import analyze_pipeline, AnalysisResult
from ado_local.models.config import LocalSettings, ServiceConnectionMapping
from ado_local.models.events import EventType, PipelineEvent
from ado_local.models.pipeline import (
    Pipeline,
    Job,
    Step,
    TaskStep,
    CheckoutStep,
    ScriptStep,
    StepStatus,
    JobStatus,
)
from ado_local.execution.engine import PipelineEngine
from ado_local.parser.yaml_loader import load_pipeline_yaml
from ado_local.parser.variable_expander import expand_variables
from ado_local.parser.expression import expand_template_expressions
from ado_local.parser.template import process_conditionals


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


def parse_pipeline_info(path: Path) -> dict[str, Any]:
    try:
        data = load_pipeline_yaml(path)
    except Exception as e:
        return {"name": path.name, "path": str(path), "parameters": [], "error": str(e)}
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
    return {
        "name": name,
        "path": str(path),
        "parameters": params,
        "error": None,
    }


class PipelineSelectScreen(Screen):
    BINDINGS = [
        Binding("r", "run", "Run"),
        Binding("a", "analyze", "Analyze"),
        Binding("f5", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
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
                Button("Analyze", id="analyze", variant="primary"),
                Button("Run", id="run", variant="success"),
                Button("Refresh", id="refresh"),
                Button("Quit", id="quit", variant="error"),
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
            self.query_one("#run", Button).disabled = True
            self.query_one("#analyze", Button).disabled = True
            return

        self.query_one("#run", Button).disabled = False
        self.query_one("#analyze", Button).disabled = False

        for p in pipelines:
            info = parse_pipeline_info(p)
            self._pipeline_info.append(info)
            label = f"[bold]{info['name']}[/]"
            if info.get("parameters"):
                label += f"  [{len(info['parameters'])} params]"
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

    def _run_selected(self) -> None:
        if self._selected_index is not None and self._selected_index < len(self._pipeline_info):
            info = self._pipeline_info[self._selected_index]
            if info.get("parameters"):
                self.app.push_screen(ParameterScreen(info))
            else:
                self.app.push_screen(RunScreen(info))

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
            Static(f"[bold]Parameters for:[/] {self._pipeline_info['name']}", id="param-title"),
            VerticalScroll(id="param-fields"),
            Horizontal(
                Button("Run", id="run-params", variant="success"),
                Button("Back", id="back"),
                id="param-buttons",
            ),
            id="param-container",
        )
        yield Footer()

    def on_mount(self) -> None:
        fields = self.query_one("#param-fields", VerticalScroll)
        for p in self._pipeline_info["parameters"]:
            req = "[red]*[/] " if p["required"] else ""
            label = f"{req}[bold]{p['name']}[/] ({p['type']})"
            if p.get("values"):
                label += f"  choices: {', '.join(p['values'])}"
            fields.mount(Static(label))
            default = p.get("default") or ""
            inp = Input(
                placeholder=str(default),
                id=f"param-{p['name']}",
                value=str(default) if default else "",
            )
            fields.mount(inp)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run-params":
            self._do_run()
        elif event.button.id == "back":
            self.app.pop_screen()

    def _do_run(self) -> None:
        params = {}
        for p in self._pipeline_info["parameters"]:
            inp = self.query_one(f"#param-{p['name']}", Input)
            val = inp.value if inp.value else p.get("default")
            if val is not None:
                params[p["name"]] = val
        info = {**self._pipeline_info, "resolved_params": params}
        self.app.push_screen(RunScreen(info))

    def action_run(self) -> None:
        self._do_run()

    def action_back(self) -> None:
        self.app.pop_screen()


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
            Button("Back", id="back", variant="primary"),
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
            data = load_pipeline_yaml(path)
            log.write(f"[green]v[/] Parsed pipeline: {path}")
        except Exception as e:
            log.write(f"[red]x[/] Failed to parse: {e}")
            return

        settings = LocalSettings()
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

    def action_back(self) -> None:
        self.app.pop_screen()


class RunResultScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "close", "Close"),
    ]

    def __init__(self, pipeline: Pipeline, duration: float) -> None:
        super().__init__()
        self._pipeline = pipeline
        self._duration = duration

    def compose(self) -> ComposeResult:
        yield Container(
            Static("[bold]Pipeline Complete[/]", id="result-title"),
            Static(f"Duration: {self._duration:.1f}s", id="result-duration"),
            Log(id="result-log"),
            Button("Close", id="close", variant="primary"),
            id="result-container",
        )

    def on_mount(self) -> None:
        log = self.query_one("#result-log", Log)
        all_succeeded = True
        for job in self._pipeline.jobs:
            for step in job.steps:
                icon = "v" if step.status == StepStatus.SUCCEEDED else "x"
                dur = f"({step.duration:.1f}s)" if step.duration else ""
                color = "green" if step.status == StepStatus.SUCCEEDED else "red"
                log.write(f"[{color}]{icon}[/] {step.task if isinstance(step, TaskStep) else type(step).__name__} {dur}")
                for line in step.logs[-5:]:
                    log.write(f"  [dim]{line}[/]")
                if step.status != StepStatus.SUCCEEDED:
                    all_succeeded = False

        if all_succeeded:
            log.write(f"\n[green bold]v All steps completed successfully[/]")
        else:
            log.write(f"\n[red bold]x Pipeline completed with failures[/]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self._do_close()

    def action_close(self) -> None:
        self._do_close()

    def _do_close(self) -> None:
        self.app.pop_screen()
        self.app.pop_screen()


class RunScreen(Screen):
    SPINNER_CHARS = "|/-\\"
    BINDINGS = [
        Binding("c", "cancel", "Cancel"),
    ]

    def __init__(self, pipeline_info: dict[str, Any]) -> None:
        super().__init__()
        self._pipeline_info = pipeline_info
        self._pipeline: Optional[Pipeline] = None
        self._engine: Optional[PipelineEngine] = None
        self._start_time: float = 0.0
        self._active_step_index: int | None = None
        self._spinner_index: int = 0
        self._cancelled: bool = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Container(
            Static(f"[bold]Running:[/] {self._pipeline_info['name']}", id="run-title"),
            Horizontal(
                Vertical(
                    Static("[bold]Steps[/]", id="steps-header"),
                    Tree("steps", id="step-tree"),
                    id="steps-panel",
                ),
                Vertical(
                    Static("[bold]Logs[/]", id="logs-header"),
                    RichLog(id="step-log", highlight=True, markup=True, wrap=True, max_lines=1000),
                    id="logs-panel",
                ),
                id="run-panels",
            ),
            Horizontal(
                Static("", id="run-footer"),
                Button("Cancel", id="cancel", variant="error"),
                id="run-footer-row",
            ),
            id="run-container",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._start_time = time.time()
        self._build_step_tree()
        self.set_interval(0.15, self._tick_spinner)
        self._log_widget = self.query_one("#step-log", RichLog)
        self._run_pipeline()

    def _build_step_tree(self) -> None:
        tree = self.query_one("#step-tree", Tree)
        tree.clear()
        params = self._pipeline_info.get("resolved_params", {})
        self._root_node = tree.root
        self._step_nodes: list[TreeNode] = []
        self._step_data: list[Any] = []
        self._step_display_names: list[str] = []

        path = Path(self._pipeline_info["path"])
        try:
            data = load_pipeline_yaml(path)
            data = expand_template_expressions(data, {"parameters": params})
            data = process_conditionals(data, {"parameters": params})
        except Exception:
            return

        steps_raw = data.get("steps", [])
        job_node = tree.root.add("default")
        for i, step_data in enumerate(steps_raw):
            name = (
                step_data.get("task")
                or step_data.get("script")
                or step_data.get("powershell")
                or ("checkout: " + str(step_data.get("checkout", "")) if "checkout" in step_data else None)
                or ("publish: " + str(step_data.get("publish", "")) if "publish" in step_data else None)
                or step_data.get("displayName")
                or f"step {i}"
            )
            if isinstance(name, dict):
                name = str(list(name.keys())[0]) if name else f"step {i}"
            display = step_data.get("displayName", name)
            node = job_node.add(f"[dim]o[/] {display}")
            self._step_nodes.append(node)
            self._step_data.append(step_data)
            self._step_display_names.append(display)

        tree.root.expand()
        job_node.expand_all()

    def _handle_pipeline_event(self, event: PipelineEvent) -> None:
        self.app.call_from_thread(self._on_pipeline_event, event)

    def _cancel_requested(self) -> bool:
        return self._cancelled

    def action_cancel(self) -> None:
        self._cancelled = True
        self.query_one("#cancel", Button).disabled = True
        log = self.query_one("#step-log", RichLog)
        log.write("[red bold]Cancel requested, stopping after current step...[/]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.action_cancel()

    def _tick_spinner(self) -> None:
        if self._active_step_index is not None:
            self._spinner_index = (self._spinner_index + 1) % 4
            icon = self.SPINNER_CHARS[self._spinner_index]
            self._update_step_node(self._active_step_index, icon, "yellow")

    def _on_pipeline_event(self, event: PipelineEvent) -> None:
        log = self.query_one("#step-log", RichLog)

        if event.event_type == EventType.STEP_START:
            self._active_step_index = event.step_index or 0
            self._spinner_index = 0
            self._update_step_node(self._active_step_index, "|", "yellow")
            log.write(f"\n[bold yellow]>> {event.step_name or ''} [/]")

        elif event.event_type == EventType.STEP_LOG:
            log.write(str(event.log_line or ""))

        elif event.event_type == EventType.STEP_COMPLETE:
            idx = event.step_index or 0
            self._active_step_index = None
            dur = f" ({event.duration:.1f}s)" if event.duration else ""
            if event.status == "succeeded" or event.status == StepStatus.SUCCEEDED.value:
                self._update_step_node(idx, "v", "green", dur)
            elif event.status == "failed" or event.status == StepStatus.FAILED.value:
                self._update_step_node(idx, "x", "red", dur)
            else:
                self._update_step_node(idx, "o", "dim", dur)

        elif event.event_type == EventType.ERROR:
            log.write(f"[red bold]ERROR: {event.message}[/]")

        elif event.event_type == EventType.WARNING:
            log.write(f"[yellow bold]WARNING: {event.message}[/]")

        elif event.event_type == EventType.PIPELINE_COMPLETE:
            duration = time.time() - self._start_time
            footer = self.query_one("#run-footer", Static)
            if self._cancelled:
                footer.update(f"[red bold]Pipeline cancelled after {duration:.1f}s[/]")
            else:
                footer.update(f"[green bold]Pipeline complete in {duration:.1f}s[/]")
            self.app.push_screen(RunResultScreen(self._pipeline, duration))

        elapsed = time.time() - self._start_time
        footer = self.query_one("#run-footer", Static)
        footer.update(f"[dim][timer] {elapsed:.1f}s[/]")

    def _update_step_node(self, index: int, icon: str, style: str, suffix: str = "") -> None:
        if 0 <= index < len(self._step_nodes):
            node = self._step_nodes[index]
            display_name = self._step_display_names[index] if index < len(self._step_display_names) else str(index)
            node.label = Text.assemble((icon, style), " ", (display_name, ""), (suffix, "dim"))

    @work(thread=True)
    async def _run_pipeline(self) -> None:
        try:
            path = Path(self._pipeline_info["path"])
            data = load_pipeline_yaml(path)

            params = self._pipeline_info.get("resolved_params", {})
            param_defs = self._pipeline_info.get("parameters", [])
            for p in param_defs:
                if p["name"] not in params and p.get("default") is not None:
                    params[p["name"]] = p["default"]

            data = expand_template_expressions(data, {"parameters": params})
            data = process_conditionals(data, {"parameters": params})

            task_specs = list(dict.fromkeys(
                step["task"] for step in data.get("steps", []) if "task" in step
            ))
            if task_specs:
                self.app.call_from_thread(self._log_widget.write, "[dim]Preparing: checking tasks...[/]")
                from ado_local.models.config import LocalSettings
                from ado_local.cache.task_cache import resolve_task
                settings = LocalSettings()
                cache_dir = Path(settings.task_cache_dir)
                for spec in task_specs:
                    self.app.call_from_thread(
                        self._log_widget.write, f"[dim]  checking {spec}...[/]"
                    )
                    resolve_task(spec, cache_dir, auto_download=True)

            steps_raw = data.get("steps", [])
            raw_vars = data.get("variables", {})
            if isinstance(raw_vars, dict):
                pipe_vars = raw_vars
            elif isinstance(raw_vars, list):
                pipe_vars = {}
                for item in raw_vars:
                    if isinstance(item, dict) and "name" in item:
                        pipe_vars[item["name"]] = item.get("value", "")
            else:
                pipe_vars = {}
            pipeline = Pipeline(name=self._pipeline_info["name"], variables=pipe_vars)
            job = Job(name="default")

            for step_data in steps_raw:
                if "task" in step_data:
                    step = TaskStep(
                        task=step_data["task"],
                        display_name=step_data.get("displayName", step_data["task"]),
                        inputs=step_data.get("inputs", {}),
                        condition=step_data.get("condition"),
                    )
                elif "script" in step_data:
                    step = ScriptStep(
                        script=step_data["script"],
                        display_name=step_data.get("displayName", "script"),
                    )
                elif "powershell" in step_data:
                    step = ScriptStep(
                        script=step_data["powershell"],
                        display_name=step_data.get("displayName", "powershell"),
                    )
                elif "checkout" in step_data:
                    step = CheckoutStep(
                        checkout=step_data.get("checkout", "self"),
                    )
                else:
                    continue
                job.steps.append(step)

            pipeline.jobs = [job]
            self._pipeline = pipeline

            settings = LocalSettings()
            self._engine = PipelineEngine(
                settings,
                event_handler=self._handle_pipeline_event,
                cancel_requested=self._cancel_requested,
            )
            self._engine.execute(pipeline)

        except Exception as e:
            self.app.call_from_thread(self._log_widget.write, f"[red bold]Pipeline failed: {e}[/]")
            import traceback
            self.app.call_from_thread(self._log_widget.write, f"[dim]{traceback.format_exc()}[/]")


class AdoLocalApp(App):
    SCREENS = {
        "select": PipelineSelectScreen,
    }
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
        height: 3;
        align: center middle;
    }
    Button {
        margin: 0 1;
    }
    #run-container {
        padding: 1;
    }
    #run-panels {
        height: 80%;
    }
    #steps-panel {
        width: 40%;
        border: solid #333;
        padding: 0 1;
    }
    #logs-panel {
        width: 60%;
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
        height: 1;
        margin: 0 0 1 0;
    }
    #step-log {
        height: 100%;
    }
    #run-footer {
        height: 1;
        color: #666;
        margin: 1 0 0 0;
    }
    #run-footer-row {
        height: 3;
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
        height: 3;
        align: center middle;
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
        align: center middle;
    }
    #result-log {
        height: 70%;
        border: solid #333;
        margin: 1 0;
    }
    """

    def on_mount(self) -> None:
        self.push_screen(PipelineSelectScreen())
