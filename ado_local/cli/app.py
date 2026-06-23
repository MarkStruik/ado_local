from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console

from ado_local import __version__
from ado_local.models.config import LocalSettings

app = typer.Typer(
    name="ado-local",
    help="Offline Azure DevOps Pipeline Runner",
    no_args_is_help=True,
)
console = Console()


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        console.print(f"[bold blue]ado-local[/] v{__version__}")
        console.print("Run [bold]ado-local run[/] to launch the TUI pipeline selector.")
        console.print("Run [bold]ado-local --help[/] for all commands.")
        raise typer.Exit()


def _load_settings(path: str) -> LocalSettings:
    settings_path = Path(path)
    if settings_path.exists():
        import json
        try:
            with open(settings_path) as f:
                data = json.load(f)
            return LocalSettings(**data)
        except Exception as e:
            console.print(f"[yellow]Warning:[/] Failed to load settings: {e}")
    return LocalSettings()


@app.command()
def analyze(
    pipeline: str = typer.Argument(..., help="Path to pipeline YAML file"),
    settings_file: str = typer.Option(".ado-local.json", "--settings", "-s", help="Settings file"),
) -> None:
    """Analyze a pipeline for missing requirements."""
    settings = _load_settings(settings_file)
    from ado_local.parser.yaml_loader import load_pipeline_yaml
    from ado_local.analysis.analyzer import analyze_pipeline

    try:
        data = load_pipeline_yaml(pipeline)
        result = analyze_pipeline(data, settings, Path(settings.task_cache_dir))

        from rich.table import Table
        from rich import box

        console.print(f"\n[bold]Analysis Report:[/] {pipeline}\n")

        if result.missing_variables:
            table = Table(box=box.ROUNDED, title="Missing Variables")
            table.add_column("Variable", style="red")
            for v in result.missing_variables:
                table.add_row(v)
            console.print(table)

        if result.missing_parameters:
            table = Table(box=box.ROUNDED, title="Missing Parameters")
            table.add_column("Parameter", style="yellow")
            for p in result.missing_parameters:
                table.add_row(p)
            console.print(table)

        if result.missing_tasks:
            table = Table(box=box.ROUNDED, title="Missing Tasks")
            table.add_column("Task", style="red")
            for t in result.missing_tasks:
                table.add_row(t)
            console.print(table)

        if result.missing_service_connections:
            table = Table(box=box.ROUNDED, title="Missing Service Connections")
            table.add_column("Connection", style="yellow")
            for c in result.missing_service_connections:
                table.add_row(c)
            console.print(table)

        if result.has_issues:
            console.print(f"\n[red bold]x {result.summary()}[/]")
            raise SystemExit(1)
        else:
            console.print(f"\n[green bold]v No issues found - ready to run![/]")

    except FileNotFoundError:
        console.print(f"[red]Pipeline file not found:[/] {pipeline}")
        raise SystemExit(1) from None
    except Exception as e:
        console.print(f"[red]Analysis failed:[/] {e}")
        raise SystemExit(1) from None


@app.command()
def prepare(
    pipeline: str = typer.Argument(..., help="Path to pipeline YAML file"),
    settings_file: str = typer.Option(".ado-local.json", "--settings", "-s", help="Settings file"),
) -> None:
    """Download tasks and tools for offline execution."""
    from ado_local.parser.yaml_loader import load_pipeline_yaml
    from ado_local.analysis.analyzer import _collect_steps
    from ado_local.prepare.downloader import download_task
    settings = _load_settings(settings_file)

    try:
        data = load_pipeline_yaml(pipeline)
        steps = _collect_steps(data)
        task_specs = sorted(set(
            s["task"] for s in steps if "task" in s
        ))

        if not task_specs:
            console.print("[yellow]No tasks found in pipeline.[/]")
            return

        cache_dir = Path(settings.task_cache_dir)
        console.print(f"[bold]Downloading {len(task_specs)} tasks...[/]\n")

        success = True
        for spec in task_specs:
            with console.status(f"Downloading {spec}..."):
                result = download_task(spec, cache_dir)
            if result:
                console.print(f"  [green]v[/] {spec}")
            else:
                console.print(f"  [red]x[/] {spec} — could not be downloaded")
                success = False

        if success:
            console.print(f"\n[green bold]All tasks downloaded successfully.[/]")
        else:
            console.print(f"\n[yellow]Some tasks could not be downloaded. You may need to provide them manually.[/]")
            raise SystemExit(1) from None

    except FileNotFoundError:
        console.print(f"[red]Pipeline file not found:[/] {pipeline}")
        raise SystemExit(1) from None
    except Exception as e:
        console.print(f"[red]Prepare failed:[/] {e}")
        raise SystemExit(1) from None


@app.command()
def run(
    pipeline: str = typer.Argument(None, help="Path to pipeline YAML file (omit for TUI selector)"),
    settings_file: str = typer.Option(".ado-local.json", "--settings", "-s", help="Settings file"),
    param: list[str] = typer.Option([], "--param", "-p", help="Parameter overrides (name=value)"),
    offline: bool = typer.Option(False, "--offline", help="Run without network access"),
    headless: bool = typer.Option(False, "--headless", help="Run without TUI (plain output)"),
) -> None:
    """Execute a pipeline locally.

    If PIPELINE is omitted, launches the TUI pipeline selector.
    """
    settings = _load_settings(settings_file)

    if headless or pipeline:
        _run_headless(pipeline, settings, param, offline)
    else:
        _run_tui()


def _run_headless(
    pipeline: str | None,
    settings: LocalSettings,
    params: list[str],
    offline: bool,
) -> None:
    if not pipeline:
        console.print("[red]Error:[/] Pipeline path required in headless mode")
        raise typer.Exit(code=1)

    from ado_local.parser.yaml_loader import load_pipeline_yaml
    from ado_local.parser.expression import expand_template_expressions
    from ado_local.parser.template import process_conditionals
    from ado_local.execution.engine import PipelineEngine
    from ado_local.models.pipeline import Pipeline, Job, TaskStep, ScriptStep, CheckoutStep

    try:
        data = load_pipeline_yaml(pipeline)

        param_dict: dict[str, str] = {}
        for p in params:
            if "=" in p:
                k, v = p.split("=", 1)
                param_dict[k] = v

        raw_params = data.get("parameters", [])
        if isinstance(raw_params, list):
            for pdef in raw_params:
                if isinstance(pdef, dict):
                    name = pdef.get("name", "")
                    if name and name not in param_dict and pdef.get("default") is not None:
                        param_dict[name] = pdef["default"]

        param_context = {"parameters": param_dict}
        data = expand_template_expressions(data, param_context)
        data = process_conditionals(data, param_context)

        steps_raw = data.get("steps", [])
        raw_vars = data.get("variables", {})
        if isinstance(raw_vars, dict):
            pipe_vars = raw_vars
        elif isinstance(raw_vars, list):
            pipe_vars = {}
            for item in raw_vars:
                if isinstance(item, dict) and "name" in item:
                    pipe_vars[item["name"]] = item.get("value", "")
                elif isinstance(item, dict) and "group" in item:
                    pass
        else:
            pipe_vars = {}
        pipe = Pipeline(name=data.get("name", Path(pipeline).stem), variables=pipe_vars)
        job = Job(name="default")

        for step_data in steps_raw:
            if "task" in step_data:
                step = TaskStep(
                    task=step_data["task"],
                    display_name=step_data.get("displayName", step_data["task"]),
                    inputs=step_data.get("inputs", {}),
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
                step = CheckoutStep(checkout=step_data.get("checkout", "self"))
            else:
                continue
            job.steps.append(step)

        pipe.jobs = [job]

        with console.status("[bold blue]Running pipeline...") as status:
            engine = PipelineEngine(settings)
            pipe = engine.execute(pipe)

        console.print(f"\n[bold]Pipeline Results:[/]")
        all_ok = True
        for job in pipe.jobs:
            for step in job.steps:
                icon = "v" if step.status.value == "succeeded" else "x"
                color = "green" if step.status.value == "succeeded" else "red"
                name = step.task if isinstance(step, TaskStep) else type(step).__name__
                dur = f" ({step.duration:.1f}s)" if step.duration else ""
                console.print(f"  [{color}]{icon}[/] {name}{dur}")
                for line in step.logs:
                    console.print(f"    [dim]{line}[/]")
                if step.status.value != "succeeded":
                    all_ok = False

        if all_ok:
            console.print(f"\n[green bold]v Pipeline succeeded[/]")
        else:
            console.print(f"\n[red bold]x Pipeline failed[/]")
            raise SystemExit(1) from None

    except FileNotFoundError:
        console.print(f"[red]Pipeline file not found:[/] {pipeline}")
        raise SystemExit(1) from None
    except Exception as e:
        console.print(f"[red]Pipeline failed:[/] {e}")
        raise SystemExit(1) from None


def _run_tui() -> None:
    from ado_local.tui.app import AdoLocalApp
    app = AdoLocalApp()
    app.run()


@app.command()
def clean(
    settings_file: str = typer.Option(".ado-local.json", "--settings", "-s", help="Settings file"),
) -> None:
    """Clean workspace directories."""
    settings = _load_settings(settings_file)
    from ado_local.execution.workspace import WorkspaceManager
    WorkspaceManager.clean_all(settings)
    console.print("[green]Workspace cleaned.[/]")


if __name__ == "__main__":
    app()
