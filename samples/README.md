# Samples

This directory contains small pipelines used to validate ADO Local behavior against Azure DevOps YAML patterns.

## .NET Console Pipeline

The `dotnet-console` sample contains a minimal .NET console app and a multi-stage Azure DevOps pipeline that is intentionally broader than the current runner supports. It is meant to drive implementation of stages, jobs, conditions, and template includes.

Useful commands:

```powershell
ado-local analyze samples/dotnet-console/azure-pipelines.yml
ado-local prepare samples/dotnet-console/azure-pipelines.yml
ado-local run samples/dotnet-console/azure-pipelines.yml --headless
ado-local run samples/dotnet-console/azure-pipelines.yml --headless --param configuration=Debug
ado-local run samples/dotnet-console/azure-pipelines.yml --headless --param runTests=false
```

You can also verify the app directly with the .NET SDK:

```powershell
dotnet build samples/dotnet-console/src/HelloPipeline/HelloPipeline.csproj --configuration Release
dotnet run --project samples/dotnet-console/src/HelloPipeline/HelloPipeline.csproj --configuration Release
```
