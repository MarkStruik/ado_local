from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, Field


class ServiceConnectionMapping(BaseModel):
    type: str = "generic"
    config: Optional[str] = None
    variables: dict[str, str] = Field(default_factory=dict)


class LocalSettings(BaseModel):
    variables: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)
    service_connections: dict[str, ServiceConnectionMapping] = Field(default_factory=dict)
    artifact_root: str = "Artifacts"
    workspace_root: str = ".ado-local"
    task_cache_dir: str = "_tasks"
    tool_cache_dir: str = "_tool"
    settings_file: str = ".ado-local.json"
