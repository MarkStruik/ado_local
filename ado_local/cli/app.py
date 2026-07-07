from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from ado_local import __version__
from ado_local.models.config import LocalSettings
from ado_local.models.pipeline import CheckoutStep, PublishStep, ScriptStep, TaskStep

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
    from ado_local.analysis.analyzer import analyze_pipeline
    from ado_local.parser.pipeline import load_and_compile_pipeline

    try:
        data = load_and_compile_pipeline(pipeline)
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
    from ado_local.parser.pipeline import collect_pipeline_tasks, load_and_compile_pipeline
    from ado_local.prepare.downloader import download_task
    settings = _load_settings(settings_file)

    try:
        data = load_and_compile_pipeline(pipeline)
        task_specs = sorted(collect_pipeline_tasks(data))

        if not task_specs:
            console.print("[yellow]No tasks found in pipeline.[/]")
            return

        cache_dir = Path(settings.task_cache_dir)
        console.print(f"[bold]Downloading {len(task_specs)} tasks...[/]\n")

        success = True
        for spec in task_specs:
            with console.status(f"Downloading {spec}..."):
                result = download_task(spec, cache_dir,
                                       azure_devops_token=settings.azure_devops_token,
                                       azure_devops_org=settings.azure_devops_org)
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

    from ado_local.parser.pipeline import load_and_compile_pipeline, parse_pipeline_model
    from ado_local.execution.engine import PipelineEngine

    try:
        param_dict: dict[str, str] = {}
        for p in params:
            if "=" in p:
                k, v = p.split("=", 1)
                param_dict[k] = v
        data = load_and_compile_pipeline(pipeline, param_dict)
        pipe = parse_pipeline_model(data, pipeline)

        with console.status("[bold blue]Running pipeline...") as status:
            engine = PipelineEngine(settings)
            pipe = engine.execute(pipe)

        console.print(f"\n[bold]Pipeline Results:[/]")
        all_ok = True
        for stage_name, job in _iter_result_jobs(pipe):
            if stage_name:
                console.print(f"  [bold]Stage:[/] {stage_name}")
            console.print(f"  [bold]Job:[/] {job.display_name or job.name}")
            for step in job.steps:
                icon = "v" if step.status.value == "succeeded" else "-" if step.status.value == "skipped" else "x"
                color = "green" if step.status.value == "succeeded" else "dim" if step.status.value == "skipped" else "red"
                name = _step_result_name(step)
                dur = f" ({step.duration:.1f}s)" if step.duration else ""
                console.print(f"    [{color}]{icon}[/] {name}{dur}")
                for line in step.logs:
                    console.print(f"      [dim]{escape(line)}[/]")
                if step.status.value not in ("succeeded", "skipped"):
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


def _iter_result_jobs(pipe):
    if pipe.stages:
        for stage in pipe.stages:
            for job in stage.jobs:
                yield stage.display_name or stage.name, job
    else:
        for job in pipe.jobs:
            yield None, job


def _step_result_name(step) -> str:
    if isinstance(step, TaskStep):
        return step.display_name or step.task
    if isinstance(step, ScriptStep):
        return step.display_name or "script"
    if isinstance(step, CheckoutStep):
        return step.display_name or f"checkout: {step.checkout}"
    if isinstance(step, PublishStep):
        return step.display_name or f"publish: {step.publish}"
    return type(step).__name__


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
