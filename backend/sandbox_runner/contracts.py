from __future__ import annotations

import uuid
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class SandboxExecutionMode(StrEnum):
    COMMAND = "command"
    PYTHON = "python"
    SHELL = "shell"


class SandboxLimits(BaseModel):
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    cpu_cores: float = Field(default=1.0, ge=0.1, le=4.0)
    memory_mb: int = Field(default=512, ge=64, le=4096)
    pids_limit: int = Field(default=64, ge=16, le=256)


class RunnerAttachment(BaseModel):
    artifact_id: uuid.UUID
    storage_key: str = Field(min_length=1, max_length=1024)
    original_name: str = Field(min_length=1, max_length=512)


class RunnerExecutionRequest(BaseModel):
    execution_id: uuid.UUID
    challenge_id: uuid.UUID
    mode: SandboxExecutionMode
    command: str | None = Field(default=None, min_length=1, max_length=20_000)
    script: str | None = Field(default=None, min_length=1, max_length=200_000)
    args: list[str] = Field(default_factory=list, max_length=64)
    attachments: list[RunnerAttachment] = Field(default_factory=list, max_length=256)
    limits: SandboxLimits = Field(default_factory=SandboxLimits)

    @model_validator(mode="after")
    def validate_payload(self) -> RunnerExecutionRequest:
        if self.mode == SandboxExecutionMode.COMMAND:
            if self.command is None or self.script is not None:
                raise ValueError("command mode requires command and forbids script")
        elif self.script is None or self.command is not None:
            raise ValueError("script mode requires script and forbids command")
        if any(len(argument) > 4096 for argument in self.args):
            raise ValueError("script argument exceeds 4096 characters")
        return self


class SandboxExecuteRequest(BaseModel):
    mode: SandboxExecutionMode
    command: str | None = Field(default=None, min_length=1, max_length=20_000)
    script: str | None = Field(default=None, min_length=1, max_length=200_000)
    args: list[str] = Field(default_factory=list, max_length=64)
    limits: SandboxLimits = Field(default_factory=SandboxLimits)

    @model_validator(mode="after")
    def validate_payload(self) -> SandboxExecuteRequest:
        RunnerExecutionRequest(
            execution_id=uuid.uuid4(),
            challenge_id=uuid.uuid4(),
            mode=self.mode,
            command=self.command,
            script=self.script,
            args=self.args,
            limits=self.limits,
        )
        return self


class GeneratedFile(BaseModel):
    relative_path: str
    size_bytes: int


class RunnerExecutionResponse(BaseModel):
    execution_id: uuid.UUID
    container_id: str
    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    duration_ms: int
    generated_files: list[GeneratedFile]
    archive_error: str | None = None
