from __future__ import annotations
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


class HandlerType(str, Enum):
    NODE = "Node"
    NODE10 = "Node10"
    NODE16 = "Node16"
    NODE20 = "Node20"
    NODE20_1 = "Node20_1"
    POWER_SHELL = "PowerShell"
    POWER_SHELL2 = "PowerShell2"
    POWER_SHELL3 = "PowerShell3"
    PROCESS = "Process"


class TaskInput(BaseModel):
    name: str
    label: Optional[str] = None
    type: str = "string"
    required: bool = False
    default: Optional[Any] = None
    options: Optional[dict[str, str]] = None
    help_markdown: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def map_default_value(cls, data: Any) -> Any:
        if isinstance(data, dict) and "defaultValue" in data and "default" not in data:
            data["default"] = data.pop("defaultValue")
        return data


class TaskExecution(BaseModel):
    handler_type: HandlerType
    target: str
    working_directory: Optional[str] = None
    argument_format: Optional[str] = None


class TaskDefinition(BaseModel):
    name: str
    friendly_name: Optional[str] = None
    description: Optional[str] = None
    author: Optional[str] = None
    help_url: Optional[str] = None
    version: Optional[str] = None
    inputs: list[TaskInput] = Field(default_factory=list)
    execution: dict[str, Any] = Field(default_factory=dict)
    source_location: Optional[str] = None

    @field_validator("version", mode="before")
    @classmethod
    def coerce_version(cls, v: Any) -> str | None:
        if v is None:
            return None
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            return f"{v.get('Major', 1)}.{v.get('Minor', 0)}.{v.get('Patch', 0)}"
        return str(v)

    def get_handlers(self) -> list[TaskExecution]:
        handlers: list[TaskExecution] = []
        for key, value in self.execution.items():
            try:
                handler_type = HandlerType(key)
            except ValueError:
                continue
            if isinstance(value, dict):
                target = value.get("target", "")
                handlers.append(
                    TaskExecution(
                        handler_type=handler_type,
                        target=target,
                        working_directory=value.get("workingDirectory"),
                        argument_format=value.get("argumentFormat"),
                    )
                )
        return handlers


class ResolvedTask(BaseModel):
    name: str
    version_spec: str
    resolved_version: str
    path: str
    definition: TaskDefinition
