from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    PIPELINE_START = "pipeline_start"
    PIPELINE_COMPLETE = "pipeline_complete"
    STAGE_START = "stage_start"
    STAGE_COMPLETE = "stage_complete"
    JOB_START = "job_start"
    JOB_COMPLETE = "job_complete"
    STEP_START = "step_start"
    STEP_LOG = "step_log"
    STEP_COMPLETE = "step_complete"
    STEP_PROGRESS = "step_progress"
    ERROR = "error"
    WARNING = "warning"


@dataclass
class PipelineEvent:
    event_type: EventType
    step_index: int | None = None
    step_name: str | None = None
    message: str | None = None
    log_line: str | None = None
    status: str | None = None
    duration: float | None = None
    exit_code: int | None = None
    metadata: dict[str, Any] | None = None


EventHandler = Callable[[PipelineEvent], None]
