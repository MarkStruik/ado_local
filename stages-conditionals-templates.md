# Implementing Stages, Conditionals, And Templates

This document describes the next parser and execution work needed to improve Azure DevOps YAML feature parity in ADO Local. The goal is to support explicit `stages`, `jobs`, Azure-compatible `condition` evaluation, and local YAML template includes such as `- template: templates.yaml`.

## Current State

- `ado_local/parser/yaml_loader.py` loads a single YAML document.
- `ado_local/parser/expression.py` expands simple scalar `${{ parameters.x }}` and `${{ variables.x }}` expressions.
- `ado_local/parser/template.py` supports a custom string-form conditional syntax, but not Azure DevOps structural insertion syntax.
- `ado_local/cli/app.py` currently converts only root-level `steps` into one default `Job` during headless runs.
- `ado_local/models/pipeline.py` already has `Pipeline`, `Stage`, `Job`, and step models.
- `ado_local/execution/engine.py` can execute `pipeline.stages`, `pipeline.jobs`, or root `pipeline.steps`, but the CLI parser does not fully populate stages/jobs yet.
- `condition` fields are stored on stages, jobs, and steps, but execution currently does not evaluate them consistently.
- `analyze` and `prepare` scan the loaded YAML directly, so template expansion must happen before their task collection pass.

## Target Azure DevOps Behavior

Azure DevOps processes YAML in a compile phase before execution. Template includes and `${{ }}` template expressions are expanded before runtime variables and before tasks run.

Important behavior to match first:

- Template files must exist before the run starts.
- Local template paths are relative to the including file unless they start with `/`.
- Template includes can appear under `steps`, `jobs`, `stages`, and `variables`.
- A step template reference looks like this:

```yaml
steps:
- template: templates.yaml
  parameters:
    runTests: true
```

- The template can define defaults and return a list of steps:

```yaml
parameters:
- name: runTests
  type: boolean
  default: false

steps:
- script: echo build
- ${{ if eq(parameters.runTests, true) }}:
  - script: echo test
```

- Azure DevOps imposes parser limits: no more than 100 YAML files and no more than 100 nesting levels.

Remote repository templates using `template.yml@repoAlias` should be deferred until local templates are solid.

## Implementation Order

### 1. Add A Compile Pipeline

Create one parser entry point that all CLI commands use before analysis, prepare, or run conversion.

Suggested API:

```python
def load_and_compile_pipeline(path: str | Path, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    ...
```

This should perform, in order:

1. Load YAML.
2. Collect pipeline parameter defaults and CLI overrides.
3. Expand local templates recursively.
4. Evaluate compile-time template expressions.
5. Apply structural conditional and loop insertion.
6. Return a fully expanded pipeline dictionary.

Use this in:

- `ado-local analyze`
- `ado-local prepare`
- `ado-local run --headless`
- TUI analysis/run paths where applicable

### 2. Parse Pipeline Shape Into Models

Move YAML-to-model conversion out of `cli/app.py` into a parser module. The CLI should not contain pipeline grammar logic.

Suggested API:

```python
def parse_pipeline_model(data: dict[str, Any], pipeline_path: Path) -> Pipeline:
    ...
```

Support these forms:

```yaml
steps:
- script: echo root steps
```

```yaml
jobs:
- job: Build
  steps:
  - script: echo job steps
```

```yaml
stages:
- stage: Build
  jobs:
  - job: Compile
    steps:
    - script: echo stage job steps
```

Parsing rules:

- Root `steps` should still become a default job for execution.
- Root `jobs` should populate `Pipeline.jobs`.
- Root `stages` should populate `Pipeline.stages`.
- Stage/job variables should be preserved and merged by the engine at execution time.
- Unknown step kinds should produce a clear warning or parser error instead of being silently skipped.

### 3. Implement Local Template Includes

Add recursive include expansion before YAML is converted to models.

Template reference input:

```yaml
- template: path/to/template.yml
  parameters:
    name: value
```

Path handling:

- `template: templates.yaml` resolves relative to the including file directory.
- `template: ../templates/steps.yml` resolves relative to the including file directory.
- `template: /templates/steps.yml` resolves relative to the root pipeline directory.
- `template: steps.yml@templates` should fail with a clear unsupported remote-template message for now.

Context-sensitive insertion:

- Under `steps`, insert the included file's `steps` value.
- Under `jobs`, insert the included file's `jobs` value.
- Under `stages`, insert the included file's `stages` value.
- Under `variables`, insert the included file's `variables` value.

Parameter handling:

- Read defaults from the included template's top-level `parameters` block.
- Merge include-site `parameters` over those defaults.
- Evaluate included YAML with a local `parameters` context.
- Remove the top-level `parameters` block from the inserted content.

Safety limits:

- Track included file count and fail after 100 files.
- Track recursion depth and fail after 100 nested includes.
- Detect cycles and show an include stack in the error message.

### 4. Implement Structural Template Expressions

Azure DevOps commonly uses mapping keys to control insertion. Support this syntax instead of only string replacement:

```yaml
steps:
- ${{ if eq(parameters.runTests, true) }}:
  - script: echo test
```

```yaml
steps:
- ${{ each project in parameters.projects }}:
  - script: dotnet build ${{ project }}
```

Expression work needed:

- Parse function calls like `eq(a, b)`, `ne(a, b)`, `and(...)`, `or(...)`, `not(...)`, `startsWith(...)`, `endsWith(...)`, `contains(...)`, `in(...)`.
- Resolve `parameters.foo`, `variables.foo`, and index syntax like `variables['Build.SourceBranch']`.
- Preserve booleans, numbers, lists, and dicts instead of converting all expression results to strings.
- If an expression occupies an entire scalar value, return the native value.
- If an expression is embedded inside a string, convert only that interpolation to a string.

Insertion rules:

- In a list, a true conditional inserts the nested list items into the parent list.
- In a list, a false conditional inserts nothing.
- In a mapping, a true conditional merges nested mapping keys into the parent mapping.
- `each` over a list repeats the nested list or mapping body for each item.
- `each` over a mapping should expose key/value pairs later; list iteration is enough for the first pass.

### 5. Implement Azure Conditions

Conditions are runtime expressions and should be evaluated immediately before a stage, job, or step runs.

Examples:

```yaml
steps:
- script: echo release only
  condition: eq(variables['Build.SourceBranch'], 'refs/heads/main')
```

```yaml
steps:
- script: echo always cleanup
  condition: always()
```

Initial function support:

- `succeeded()`
- `failed()`
- `always()`
- `canceled()`
- `succeededOrFailed()`
- `eq()`
- `ne()`
- `and()`
- `or()`
- `not()`
- `in()`
- `startsWith()`
- `endsWith()`
- `contains()`

Execution behavior:

- Default condition is `succeeded()`.
- Disabled steps should still skip before condition evaluation.
- A false condition should mark the stage/job/step as skipped, not failed.
- `continueOnError` should allow later default-condition steps to continue using Azure-like `SucceededWithIssues` semantics eventually. For the first pass, keep existing failure behavior unless needed by tests.
- Stage and job conditions should be evaluated before entering their children.

### 6. Update Analysis And Prepare

`analyze` and `prepare` must use compiled YAML, otherwise tasks hidden in templates will be missed.

Required changes:

- Compile the pipeline before `_collect_steps` runs.
- Update `_collect_steps` to traverse root steps, jobs, and stages from the expanded YAML.
- Include tasks inserted by templates.
- Include variables inserted by variable templates.
- Report missing local template files as analysis errors.

### 7. Add Tests And Samples

Add tests around the parser before changing execution behavior heavily.

Recommended fixtures:

- Root `steps` pipeline still works.
- Root `jobs` pipeline creates multiple jobs.
- Root `stages` pipeline creates stages with jobs and steps.
- Step template include inserts tasks into `steps`.
- Template include with parameters overrides defaults.
- Nested local template include works.
- Missing template file returns a useful error.
- Include cycle returns a useful error.
- Structural `${{ if }}` inserts or skips steps.
- Structural `${{ each }}` expands a list of steps.
- Step condition `false` skips the step.
- Step condition `always()` runs after a failed prior step.

## Non-Goals For The First Pass

- Remote repository templates with `@repoAlias`.
- `extends` templates.
- Deployment jobs.
- Matrix strategies.
- Full Azure expression language coverage.
- Required template approvals or security enforcement.
- Perfect Azure timeline semantics.

## Acceptance Criteria

The feature should be considered ready when:

- `ado-local analyze` sees tasks defined inside local step templates.
- `ado-local prepare` downloads tasks defined inside local step templates.
- `ado-local run --headless` can run a root `stages` pipeline with explicit jobs.
- `ado-local run --headless` can run a root `steps` pipeline that includes `templates.yaml`.
- Template parameters work for strings, booleans, objects, and step lists in common cases.
- False conditions skip stages, jobs, and steps without failing the pipeline.
- Missing templates and recursive templates fail with clear messages.
