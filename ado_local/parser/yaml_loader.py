from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

yaml = YAML(typ="safe")


def load_pipeline_yaml(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Pipeline file not found: {path}")
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.load(f)
    except YAMLError as e:
        raise ValueError(f"Failed to parse YAML: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("Pipeline YAML must be a mapping (dictionary)")
    return data
