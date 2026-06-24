from __future__ import annotations

import re


ANSI_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-Z\\-_])"
)


def normalize_log_line(line: str) -> str:
    """Remove terminal control sequences from process output logs."""
    return ANSI_RE.sub("", line)
