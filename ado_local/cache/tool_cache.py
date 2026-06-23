from __future__ import annotations

from pathlib import Path


class ToolCache:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir

    def find_tool(self, name: str, version: str) -> Path | None:
        tool_path = self.cache_dir / name / version
        if tool_path.exists():
            return tool_path
        return None

    def get_path(self, name: str, version: str = "") -> Path:
        return self.cache_dir / name / version if version else self.cache_dir / name
