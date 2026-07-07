# ADO Local

ADO Local is a local Azure DevOps YAML pipeline runner. It is designed to shorten the pipeline feedback loop by running Azure DevOps-style pipeline YAML on your machine, while emulating the parts of the Azure Pipelines agent that tasks expect.

The project is a Python CLI package named `ado-local`. It parses pipeline YAML, prepares local workspaces, runs script and task steps, processes Azure DevOps logging commands, and stores artifacts locally instead of uploading them to Azure DevOps.

## What It Does

- Runs Azure DevOps YAML pipelines locally.
- Supports `script`, `powershell`/`pwsh`, `checkout`, and task steps.
- Resolves variables and basic template parameters.
- Emulates Azure agent directories such as source, artifact staging, binaries, and temp folders.
- Executes cached Azure task packages from a local task cache.
- Provides commands to analyze, prepare, run, and clean local pipeline state.
- Includes an optional terminal UI when running without a pipeline argument.

## Requirements

- Python 3.11 or newer
- Git
- Node.js, if running Node-based Azure task handlers
- PowerShell 7+, if running PowerShell-based task handlers
- Any tools required by the pipeline itself, such as the .NET SDK

## Install Locally

Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install the package in editable mode:

```powershell
python -m pip install --upgrade pip
python -m pip install -e .
```

After installation, the CLI is available as:

```powershell
ado-local --help
```

You can also run it as a module:

```powershell
python -m ado_local --help
```

## Run A Pipeline

Run the included sample pipeline:

```powershell
ado-local run sample-pipeline.yml
```

Run with plain console output instead of the terminal UI path:

```powershell
ado-local run sample-pipeline.yml --headless
```

Override a pipeline parameter:

```powershell
ado-local run sample-pipeline.yml --param configuration=Debug
```

Run using only locally cached task packages:

```powershell
ado-local run sample-pipeline.yml --offline
```

If no pipeline path is provided, `ado-local run` launches the TUI pipeline selector.

## Analyze A Pipeline

Use `analyze` before running to detect missing variables, parameters, tasks, or service connections:

```powershell
ado-local analyze sample-pipeline.yml
```

## Prepare Task Cache

Use `prepare` to download referenced Azure task packages into the configured task cache:

```powershell
ado-local prepare sample-pipeline.yml
```

By default, task packages are stored under `~/.ado-local/tasks`.

## Clean Local Workspace

Remove local workspace run directories:

```powershell
ado-local clean
```

## Local Settings

The CLI reads `.ado-local.json` by default. This file is intentionally ignored by Git because it can contain local paths and tokens.

Example:

```json
{
  "variables": {
    "BuildConfiguration": "Release"
  },
  "parameters": {},
  "service_connections": {
    "example-nuget-feed": {
      "type": "nuget",
      "config": "C:/configs/nuget.config"
    }
  },
  "artifact_root": "Artifacts",
  "workspace_root": ".ado-local",
  "task_cache_dir": "~/.ado-local/tasks",
  "tool_cache_dir": "_tool",
  "checkout_mode": "local",
  "azure_devops_org": null,
  "azure_devops_project": null,
  "azure_devops_token": null
}
```

Use a different settings file with:

```powershell
ado-local run sample-pipeline.yml --settings path/to/settings.json
```

## Build The Package

Install the build frontend:

```powershell
python -m pip install build
```

Build source and wheel distributions:

```powershell
python -m build
```

Build outputs are written to `dist/`.

## Project Structure

```text
ado_local/
  analysis/      Pipeline pre-flight checks
  artifacts/     Local artifact publishing
  cache/         Task and tool cache helpers
  cli/           Typer CLI entrypoint
  connections/   Service connection mapping
  execution/     Pipeline engine, task runner, checkout, workspace
  logging/       Azure DevOps logging command parsing
  models/        Pydantic data models
  parser/        YAML, template, expression, and variable parsing
  prepare/       Task download/preparation helpers
  tui/           Terminal UI
sample-pipeline.yml
project.md
pyproject.toml
```

## Development Notes

- Runtime dependencies are declared in `pyproject.toml`.
- There is currently no substantive automated test suite in the repository.
- Generated folders such as `.ado-local/`, `Artifacts/`, `_tool/`, `_tasks/`, `dist/`, and virtual environments are ignored by Git.
- `project.md` contains the broader project design and implementation plan.
