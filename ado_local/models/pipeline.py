from __future__ import annotations
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class TaskStep(BaseModel):
    task: str
    display_name: Optional[str] = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    condition: Optional[str] = None
    enabled: bool = True
    timeout_in_minutes: Optional[int] = None
    env: dict[str, Any] = Field(default_factory=dict)
    status: StepStatus = StepStatus.PENDING
    logs: list[str] = Field(default_factory=list)
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    exit_code: Optional[int] = None

    @property
    def duration(self) -> Optional[float]:
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return None


class CheckoutStep(BaseModel):
    checkout: str = "self"
    submodules: bool = False
    persist_credentials: bool = False
    lfs: bool = False
    path: Optional[str] = None
    status: StepStatus = StepStatus.PENDING
    logs: list[str] = Field(default_factory=list)
    start_time: Optional[float] = None
    end_time: Optional[float] = None

    @property
    def duration(self) -> Optional[float]:
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return None


class ScriptStep(BaseModel):
    script: str
    display_name: Optional[str] = None
    condition: Optional[str] = None
    enabled: bool = True
    env: dict[str, Any] = Field(default_factory=dict)
    status: StepStatus = StepStatus.PENDING
    logs: list[str] = Field(default_factory=list)
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    exit_code: Optional[int] = None

    @property
    def duration(self) -> Optional[float]:
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return None


Step = TaskStep | CheckoutStep | ScriptStep


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job(BaseModel):
    name: str
    display_name: Optional[str] = None
    pool: Optional[str] = None
    steps: list[Step] = Field(default_factory=list)
    variables: dict[str, Any] = Field(default_factory=dict)
    condition: Optional[str] = None
    status: JobStatus = JobStatus.PENDING
    start_time: Optional[float] = None
    end_time: Optional[float] = None

    @property
    def duration(self) -> Optional[float]:
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return None


class Stage(BaseModel):
    name: str
    display_name: Optional[str] = None
    jobs: list[Job] = Field(default_factory=list)
    condition: Optional[str] = None
    variables: dict[str, Any] = Field(default_factory=dict)
    status: JobStatus = JobStatus.PENDING


class Pipeline(BaseModel):
    name: Optional[str] = None
    trigger: Optional[Any] = None
    pool: Optional[Any] = None
    variables: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)
    stages: list[Stage] = Field(default_factory=list)
    jobs: list[Job] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    workspace_dir: Optional[str] = None
    run_id: Optional[str] = None
