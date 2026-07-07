# .NET Console Sample

This sample is a small .NET console application with an Azure DevOps-style pipeline for ADO Local feature testing.

The pipeline exercises:

- Explicit `stages` and `jobs`.
- Step templates with `- template: templates/*.yml`.
- Template parameters.
- Compile-time parameter expressions.
- Stage and step `condition` fields.
- `DotNetCoreCLI@2` task steps.
- Script and `pwsh` steps.
- Local artifact publishing with `publish:`.

The current runner does not support every feature used here yet. Use this sample as an implementation target for stage, condition, and template parity.

Run from the repository root:

```powershell
ado-local analyze samples/dotnet-console/azure-pipelines.yml
ado-local prepare samples/dotnet-console/azure-pipelines.yml
ado-local run samples/dotnet-console/azure-pipelines.yml --headless
```

Parameter examples:

```powershell
ado-local run samples/dotnet-console/azure-pipelines.yml --headless --param configuration=Debug
ado-local run samples/dotnet-console/azure-pipelines.yml --headless --param runTests=false
ado-local run samples/dotnet-console/azure-pipelines.yml --headless --param publishArtifacts=false
```
