from __future__ import annotations
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


class VariableType(str, Enum):
    STATIC = "static"
    RUNTIME = "runtime"
    COUNTER = "counter"
    PREDEFINED = "predefined"


class Variable(BaseModel):
    name: str
    value: Any = None
    variable_type: VariableType = VariableType.STATIC
    expression: Optional[str] = None
    description: Optional[str] = None


class Parameter(BaseModel):
    name: str
    value: Any = None
    default: Any = None
    parameter_type: str = "string"
    required: bool = False
    values: Optional[list[Any]] = None


class CounterState(BaseModel):
    counters: dict[str, int] = Field(default_factory=dict)

    def get_next(self, name: str, seed: int = 1) -> int:
        key = f"counter:{name}"
        current = self.counters.get(key, seed - 1)
        next_val = current + 1
        self.counters[key] = next_val
        return next_val


PREDEFINED_VARIABLES: dict[str, str] = {
    "Build.SourcesDirectory": "$(System.DefaultWorkingDirectory)/s",
    "Build.StagingDirectory": "$(System.DefaultWorkingDirectory)/a",
    "Build.BinariesDirectory": "$(System.DefaultWorkingDirectory)/b",
    "Build.ArtifactStagingDirectory": "$(System.DefaultWorkingDirectory)/a",
    "Agent.TempDirectory": "$(System.DefaultWorkingDirectory)/_temp",
    "Agent.ToolsDirectory": "$(System.DefaultWorkingDirectory)/_tool",
    "Agent.WorkFolder": "$(System.DefaultWorkingDirectory)",
    "Agent.HomeDirectory": "",
    "Agent.Id": "1",
    "Agent.Name": "ado-local",
    "Agent.MachineName": "localhost",
    "Agent.Version": "4.0.0",
    "System.DefaultWorkingDirectory": "",
    "System.TeamProject": "local",
    "System.TeamFoundationCollectionUri": "http://localhost:8080/",
    "System.ArtifactsDirectory": "$(System.DefaultWorkingDirectory)/a",
    "Build.BuildId": "1",
    "Build.BuildNumber": "1",
    "Build.DefinitionName": "local-pipeline",
    "Build.Repository.LocalPath": "$(System.DefaultWorkingDirectory)/s",
    "Build.Repository.Name": "local",
    "Build.Repository.Provider": "git",
}
