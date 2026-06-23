
# ADO-Local: Offline Azure DevOps Pipeline Runner

## Project Design Document

### Vision

Build a local execution environment capable of running Azure DevOps YAML pipelines using the **actual Azure Pipeline task implementations**, while replacing only Azure DevOps server-side services with local equivalents.

The goal is not to create another build system.

The goal is to reproduce the Azure Pipelines Agent runtime locally.

---

# Problem Statement

Current Azure DevOps pipeline development is slow because:

* Pipeline modifications require commits
* Pipelines require pushes
* Hosted agents must be provisioned
* Tool installation happens repeatedly
* Failures are discovered late
* Debugging requires log inspection

Typical feedback loop:

```text
Edit YAML
Commit
Push
Wait 2-10 minutes
Pipeline starts
Wait another 5-20 minutes
Failure
Repeat
```

Desired feedback loop:

```text
Edit YAML
Run locally
See failure in seconds
Fix
Run again
```

---

# Core Design Principle

We do NOT want:

```text
Fake DotNet task
Fake NuGet task
Fake NodeTool task
Fake UseDotNet task
```

Instead:

```text
Run the actual task package
Run the actual task handler
Run the actual task code
```

The local runner should behave like a lightweight Azure Agent.

---

# High-Level Architecture

```text
┌──────────────────────┐
│ Azure YAML Pipeline  │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│ Pipeline Parser      │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│ Analysis Engine      │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│ Execution Engine     │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│ Task Runtime Host    │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│ Real Azure Tasks     │
└──────────────────────┘
```

---

# Major Components

## 1. YAML Loader

Responsible for:

* Reading YAML
* Template expansion
* Parameter processing
* Variable expansion
* Expression evaluation

Libraries:

```python
ruamel.yaml
or
PyYAML
```

---

## 2. Workspace Manager

Emulates Azure Agent working directories.

Azure uses:

```text
_work/
 └── 1/
      ├── s/
      ├── a/
      ├── b/
      └── _temp/
```

Local equivalent:

```text
ado-local/
 └── work/
      └── run-<guid>/
           ├── s/
           ├── a/
           ├── b/
           └── _temp/
```

---

# Checkout Emulation

Pipeline:

```yaml
- checkout: self
```

Local behavior:

```bash
git clone
git checkout
git submodule update --init --recursive
```

Support:

```yaml
submodules: true
persistCredentials: false
lfs: false
```

---

# Variable System

Must support:

## Static Variables

```yaml
variables:
  major: 3
```

---

## Runtime Variables

```yaml
build: $[counter(variables['minor'], 1)]
```

Need expression engine.

Store counters:

```json
{
  "counter:minor": 47
}
```

---

## Variable Expansion

Support:

```text
$(Build.SourcesDirectory)
$(major)
$(solution)
```

Expansion occurs before task execution.

---

# Parameter System

Support:

```yaml
parameters:
- name: configuration
  default: Release
```

CLI:

```bash
ado-local run pipeline.yml \
  --param configuration=Debug
```

---

# Missing Variable Detection

During analysis phase:

```bash
ado-local analyze pipeline.yml
```

Detect:

```text
Missing variable:
  NexusPassword

Missing parameter:
  Environment

Missing service connection:
  isrsc-nuget-read
```

Prompt user.

---

# Analysis Engine

Purpose:

Determine everything required before execution.

Produces:

```json
{
  "missingVariables": [],
  "missingTasks": [],
  "missingTools": [],
  "missingServiceConnections": []
}
```

---

# Task System

Most important subsystem.

---

## Task Discovery

Example:

```yaml
- task: DotNetCoreCLI@2
```

Resolve:

```text
DotNetCoreCLI
Version 2
```

Find:

```text
_tasks/
  DotNetCoreCLI/
     2.240.0/
```

---

# Task Package Structure

Typical:

```text
DotNetCoreCLI/
 ├── task.json
 ├── dotnetcore.js
 └── node_modules/
```

---

# task.json Processing

Read:

```json
{
  "inputs": [],
  "execution": {}
}
```

Determine:

```text
handler type
required inputs
runtime
```

---

# Supported Handlers

## Node

```json
{
  "Node20": {
    "target": "dotnetcore.js"
  }
}
```

Run:

```bash
node dotnetcore.js
```

---

## PowerShell

```json
{
  "PowerShell3": {
      "target": "script.ps1"
  }
}
```

Run:

```bash
pwsh script.ps1
```

---

## Process

```json
{
  "Process": {
      "target": "tool.exe"
  }
}
```

Run executable.

---

# Input Injection

Azure Agent injects task inputs as:

```text
INPUT_COMMAND
INPUT_PROJECTS
INPUT_ARGUMENTS
```

Example:

```yaml
inputs:
  command: build
```

becomes

```text
INPUT_COMMAND=build
```

---

# Environment Variables

Provide:

```text
BUILD_SOURCESDIRECTORY
BUILD_STAGINGDIRECTORY
AGENT_TEMPDIRECTORY
SYSTEM_DEFAULTWORKINGDIRECTORY
```

And many more.

---

# Logging Command Processor

Azure tasks communicate through stdout.

Example:

```text
##vso[task.setvariable variable=Version]1.2.3
```

Need parser.

---

# Supported Commands

## task.setvariable

```text
##vso[task.setvariable]
```

---

## task.complete

```text
##vso[task.complete]
```

---

## task.logissue

```text
##vso[task.logissue]
```

---

## build.updatebuildnumber

```text
##vso[build.updatebuildnumber]
```

---

# Service Connections

Azure-only concept.

Need local mapping.

Example:

```yaml
externalFeedCredentials:
  isrsc-nuget-read
```

Local mapping:

```json
{
  "serviceConnections": {
    "isrsc-nuget-read": {
      "type": "nuget",
      "config": "c:/configs/nuget.config"
    }
  }
}
```

---

# Tool Cache

Mirror Azure tool cache.

```text
_tool/
 ├── node/
 ├── dotnet/
 ├── nuget/
 ├── java/
 └── python/
```

---

# Online Prepare Command

```bash
ado-local prepare pipeline.yml
```

Downloads:

* Tasks
* Tool installers
* Metadata

Stores in cache.

---

# Offline Run

```bash
ado-local run pipeline.yml --offline
```

Uses cache only.

No network allowed.

---

# Artifact System

Azure:

```yaml
publish: Installer/setup/
artifact: Installer
```

Local:

```text
Artifacts/
 └── Installer/
```

Copy only.

No uploads.

---

# Pipeline Execution Model

Execution engine:

```text
for step in steps:
    resolve variables
    execute task
    parse logs
    update state
```

---

# Failure Model

Any of:

```text
exit code != 0
task.complete failed
task.logissue error
```

causes pipeline failure.

---

# Local Settings File

```json
{
  "variables": {},
  "parameters": {},
  "serviceConnections": {},
  "artifactRoot": "C:/Artifacts"
}
```

---

# CLI Design

## Analyze

```bash
ado-local analyze pipeline.yml
```

---

## Prepare

```bash
ado-local prepare pipeline.yml
```

---

## Run

```bash
ado-local run pipeline.yml
```

---

## Clean

```bash
ado-local clean
```

---

# Recommended Technology Stack

## Python

Reasons:

* YAML support
* Process execution
* Cross-platform
* Easy packaging

---

## Libraries

```text
Typer
Rich
ruamel.yaml
pydantic
jinja2
gitpython
```

---

# Future Features

## Parallel Jobs

```yaml
strategy:
  matrix:
```

---

## Containers

```yaml
container:
```

---

## Deployment Jobs

```yaml
deployment:
```

---

## Environment Approvals

Mock locally.

---

## Interactive Debugger

```bash
ado-local run --break-before-step
```

Possible future capability:

```text
Step 12:
DotNetCoreCLI@2

[c] continue
[s] skip
[e] edit variables
[q] quit
```

---

# MVP Scope

The MVP should support:

* YAML parsing
* Variable expansion
* Parameters
* Checkout
* Real task execution
* Task.json parsing
* Node handlers
* PowerShell handlers
* Process handlers
* Logging commands
* Artifact publishing
* Service connection mapping
* Offline cache

Nothing more.

If this MVP works, your example pipeline should execute locally with the same task implementations Azure DevOps uses, while replacing only Azure-hosted services (artifacts, feeds, service connection lookup, timeline APIs) with local equivalents.

---

# Master Implementation Plan

## Phase 1: Project Scaffolding & Core Data Models

**Goal**: Establish project structure, dependencies, and foundational data types.

- Initialize Python package (`ado-local` / `ado_local`)
- Create `pyproject.toml` with dependencies (typer, rich, ruamel.yaml, pydantic, jinja2, gitpython)
- Define core Pydantic models:
  - `Pipeline` — top-level pipeline definition
  - `Stage`, `Job`, `Step` — pipeline hierarchy
  - `Task` — task reference with inputs
  - `Variable` — static & runtime variables
  - `Parameters` — parameter definitions
  - `Settings` — local settings file schema
  - `ServiceConnection` — connection mapping
- Implement config/settings loader (JSON file)
- Create `__init__.py` files across all packages

---

## Phase 2: CLI Skeleton

**Goal**: Implement command-line interface with all subcommands.

- Create `cli/app.py` with Typer app
- Implement stubs for:
  - `ado-local analyze <pipeline.yml>`
  - `ado-local prepare <pipeline.yml>`
  - `ado-local run <pipeline.yml> [--offline] [--param]`
  - `ado-local clean`
- Add Rich console output for help/status

---

## Phase 3: YAML Loader & Parser

**Goal**: Parse Azure DevOps YAML pipelines accurately.

- Implement YAML loading with `ruamel.yaml` (preserves comments/ordering)
- Process `parameters` block — build parameter set with defaults
- Process `variables` block — static variables
- Implement template expression evaluation:
  - `${{ parameters.xxx }}`
  - `${{ variables.xxx }}`
  - `${{ if condition }}` / `${{ else }}` / `${{ endif }}`
  - `${{ each }}` loops
- Implement runtime expression evaluation:
  - `$[counter(variables.xxx, seed)]`
  - `$[format(...)]`
  - `$[lower(...)]` / `$[upper(...)]`
- Implement variable expansion: `$(VariableName)` → value
- Detect and report missing required parameters

---

## Phase 4: Workspace Manager & Checkout

**Goal**: Emulate Azure Pipelines agent working directories.

- Create `ado-local/work/run-<guid>/` directory structure
- Emulate `s/` (sources), `a/` (artifacts staging), `b/` (binaries), `_temp/` (temp)
- Implement checkout step:
  - `checkout: self` — clone current repo into `s/`
  - Support `submodules`, `persistCredentials`, `lfs` flags
  - Support `checkout: none`
- Clean workspace on `ado-local clean`
- Implement `gitpython`-based clone/checkout

---

## Phase 5: Variable System

**Goal**: Full variable resolution and expansion engine.

- Implement variable container with scoping (pipeline-level, job-level, step-level)
- Resolve static variables at parse time
- Resolve runtime variables (counter, etc.) before each step
- Implement `$(Build.SourcesDirectory)` and other predefined variables
- Counter storage (JSON file in workspace)
- Variable expansion in task inputs
- Variable expansion in condition expressions

---

## Phase 6: Task System

**Goal**: Discover, resolve, and prepare task packages.

- Implement `_tasks/` cache directory discovery
- Implement task resolution:
  - `DotNetCoreCLI@2` → find `_tasks/DotNetCoreCLI/2.*/`
  - Handle version matching (major.minor.patch)
- Implement `task.json` parsing:
  - Extract `inputs` schema (name, type, required, default)
  - Extract `execution` handlers (Node, PowerShell3, Process)
  - Validate required inputs
- Validate task availability (detect missing tasks in analysis)

---

## Phase 7: Execution Engine

**Goal**: Run real Azure task packages with proper environment.

- Implement step-by-step execution loop:
  ```
  for step in steps:
      resolve variables for step
      inject inputs as environment variables (INPUT_*)
      provision Azure agent environment variables
      determine handler type from task.json
      execute handler
      capture stdout/stderr
      process logging commands
      check exit code
      update pipeline state
  ```
- **Node Handler**:
  - Run `node <task-path>/<target-script>`
  - Inject `INPUT_*` env vars
  - Set `AGENT_*`, `BUILD_*`, `SYSTEM_*`, `RELEASE_*` env vars
- **PowerShell Handler**:
  - Run `pwsh <task-path>/<target-script>`
  - Same env var injection
- **Process Handler**:
  - Run `<task-path>/<target>` executable directly
  - Same env var injection
- Implement input injection: `INPUT_COMMAND=build` etc.

---

## Phase 8: Logging Command Processor

**Goal**: Parse and respond to `##vso[...]` logging commands from task output.

- Implement regex-based parser for `##vso[commandName key=value]message`
- Support:
  - `##vso[task.setvariable variable=xxx]value` — update pipeline variables
  - `##vso[task.complete result=Succeeded/Failed]` — task completion status
  - `##vso[task.logissue type=error/warning]message` — collect issues
  - `##vso[build.updatebuildnumber]value` — build number
  - `##vso[task.addattachment]` — artifact attachment
  - `##vso[task.uploadsummary]` — summary markdown
- Update pipeline state in response to commands
- Surface errors/warnings in console output with Rich formatting

---

## Phase 9: Service Connections & Artifacts

**Goal**: Map Azure service connections locally and handle artifacts.

- Load service connections from local settings JSON
- Map service connection names to configuration:
  - NuGet → nuget.config path
  - npm → .npmrc
  - Generic → env vars
- Inject service connection values as environment variables during task execution
- Implement artifact publishing:
  - `publish: <path>` → copy to local `Artifacts/<artifact-name>/`
  - Handle `artifact: <name>` and `artifactType: <type>`
- Print artifact output paths for user

---

## Phase 10: Analysis Engine

**Goal**: Pre-flight check to detect missing requirements.

- Implement `ado-local analyze` command
- Parse pipeline YAML (full pass through parser)
- Detect:
  - Missing variables (referenced but not defined)
  - Missing parameters (referenced but not provided)
  - Missing tasks (not in cache / not downloadable)
  - Missing service connections (referenced but not mapped)
  - Missing tools (Node, .NET, Python versions)
- Print analysis report with Rich tables
- Exit with appropriate code

---

## Phase 11: Prepare & Offline Cache

**Goal**: Download tasks and tools for offline execution.

- Implement `ado-local prepare` command
- Scan pipeline for all referenced tasks
- Download task packages from Azure DevOps Marketplace / GitHub
- Store in `_tasks/<task-name>/<version>/` structure
- Download required tool installers
- Store in `_tool/<tool-name>/<version>/`
- Implement `--offline` flag for `run` command
- Fail in offline mode if cache miss occurs

---

## Phase 12: Pipeline TUI (Terminal UI)

**Goal**: Azure-DevOps-style live pipeline visualization in the terminal.

- Add `textual` dependency for interactive TUI framework
- Build a TUI screen that mimics the Azure DevOps pipeline run view:
  ```
  ┌─────────────────────────────────────────────────────────┐
  │  ado-local run — pipeline.yml                    #42   │
  ├─────────────────────────────────────────────────────────┤
  │  ● Build                                                │
  │  ├── ✓ Initialize Agent                   0.5s          │
  │  ├── ✓ Checkout                           2.1s          │
  │  ├── ● DotNetCoreCLI@2                   12.3s  ◄──    │
  │  │   ℹ Running dotnet build...                         │
  │  │   ℹ Build succeeded.                                 │
  │  │                                                      │
  │  ├── ○ NuGetCommand@2                    pending        │
  │  ├── ○ VSBuild@1                         pending        │
  │  └── ○ PublishBuildArtifacts@1           pending        │
  ├─────────────────────────────────────────────────────────┤
  │  [ 00:17:42 ]  ● running  ●●●○○○○○○○  3/7 steps       │
  └─────────────────────────────────────────────────────────┘
  ```
- Components:
  - Pipeline header (name, run ID, duration)
  - Job/step tree with status icons (○ pending, ● running, ✓ succeeded, ✗ failed)
  - Log panel for currently selected/executing step
  - Footer with elapsed time and progress bar
- Event-driven architecture:
  - Execution engine emits events via callback/asyncio
  - TUI subscribes to events and updates live
  - Events: `step_start`, `step_log`, `step_end`, `pipeline_complete`
- Support `--headless` flag for CI/scripting use (plain output)
- Keyboard shortcuts:
  - `↑`/`↓` — navigate steps
  - `l` — toggle log view
  - `q` — quit
  - `r` — rerun
- Color-coded status indicators matching Azure DevOps theme

---

## Phase 13: Polish & Testing

**Goal**: Production-ready CLI with error handling and test coverage.

- Comprehensive error handling:
  - Pipeline parse errors with location info
  - Task execution failures with exit codes
  - Missing cache entries with remediation suggestions
- Rich-formatted console output:
  - Step execution progress
  - Success/failure indicators
  - Task timings
- Add `sample-pipeline.yml` for testing
- Write unit tests for:
  - Variable expansion
  - Expression evaluation
  - Logging command parser
  - Task resolution
  - Input injection
- Write integration test with a real Node task
- Test on Windows, macOS, Linux

---

## Implementation Order

```
Phase 1  ─►  Phase 2  ─►  Phase 3  ─►  Phase 4
                                         │
                                         ▼
                                  Phase 5  ─►  Phase 6
                                         │
                                         ▼
                                  Phase 7  ─►  Phase 8
                                         │
                                         ▼
                                  Phase 9  ─►  Phase 10
                                         │
                                         ▼
                                  Phase 11  ─►  Phase 12
```

Dependencies flow top-to-bottom. Each phase builds on the previous.

**Current Status**: Phases 1-12 complete. MVP functional.
