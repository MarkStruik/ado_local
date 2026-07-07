from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from ado_local.models.config import LocalSettings


class WorkspaceManager:
    def __init__(self, settings: LocalSettings, run_id: str | None = None) -> None:
        self.settings = settings
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.root = Path(settings.workspace_root).resolve() / "work" / f"run-{self.run_id}"

    @classmethod
    def from_existing(cls, settings: LocalSettings, root: str | Path) -> WorkspaceManager:
        root = Path(root).resolve()
        run_id = root.name.removeprefix("run-") if root.name.startswith("run-") else uuid.uuid4().hex[:12]
        self = cls.__new__(cls)
        self.settings = settings
        self.run_id = run_id
        self.root = root
        return self

    def create(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        for subdir in ["s", "a", "b", "_temp"]:
            (self.root / subdir).mkdir(parents=True, exist_ok=True)
        return self.root

    @property
    def sources_dir(self) -> Path:
        return self.root / "s"

    @property
    def staging_dir(self) -> Path:
        return self.root / "a"

    @property
    def binaries_dir(self) -> Path:
        return self.root / "b"

    @property
    def temp_dir(self) -> Path:
        return self.root / "_temp"

    @property
    def tool_dir(self) -> Path:
        return Path(self.settings.tool_cache_dir)

    @property
    def task_dir(self) -> Path:
        return Path(self.settings.task_cache_dir)

    def clean(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    @staticmethod
    def clean_all(settings: LocalSettings) -> None:
        work_root = Path(settings.workspace_root) / "work"
        if work_root.exists():
            for run_dir in work_root.iterdir():
                if run_dir.is_dir():
                    shutil.rmtree(run_dir)

    def get_env(self) -> dict[str, str]:
        return {
            "AGENT_TEMPDIRECTORY": str(self.temp_dir.resolve()),
            "AGENT_TOOLSDIRECTORY": str(self.tool_dir.resolve()),
            "AGENT_WORKFOLDER": str(self.root.parent.resolve()),
            "AGENT_BUILDDIRECTORY": str(self.root.resolve()),
            "BUILD_SOURCESDIRECTORY": str(self.sources_dir.resolve()),
            "BUILD_STAGINGDIRECTORY": str(self.staging_dir.resolve()),
            "BUILD_BINARIESDIRECTORY": str(self.binaries_dir.resolve()),
            "BUILD_ARTIFACTSTAGINGDIRECTORY": str(self.staging_dir.resolve()),
            "SYSTEM_DEFAULTWORKINGDIRECTORY": str(self.root.resolve()),
            "SYSTEM_ARTIFACTSDIRECTORY": str(self.staging_dir.resolve()),
        }
