from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ado_local.models.config import ServiceConnectionMapping


class ServiceConnectionManager:
    def __init__(self, connections: dict[str, ServiceConnectionMapping]) -> None:
        self.connections = connections

    def get_env(self, connection_name: str) -> dict[str, str]:
        mapping = self.connections.get(connection_name)
        if not mapping:
            return {}
        env = dict(mapping.variables)
        if mapping.config and mapping.type == "nuget":
            env["NUGET_CONFIG_FILE"] = mapping.config
        elif mapping.config and mapping.type == "npm":
            env["NPM_CONFIG_USERCONFIG"] = mapping.config
        return env

    def get_all_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for name, mapping in self.connections.items():
            for key, value in mapping.variables.items():
                env[f"SC_{name.upper()}_{key}"] = value
        return env

    def has_connection(self, name: str) -> bool:
        return name in self.connections

    def missing_connections(self, referenced: set[str]) -> list[str]:
        return [c for c in referenced if c not in self.connections]
