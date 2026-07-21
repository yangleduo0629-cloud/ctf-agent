import pytest
from pydantic import ValidationError

from backend.sandbox_runner.contracts import (
    SandboxExecuteRequest,
    SandboxExecutionMode,
    SandboxLimits,
)


def test_command_and_script_modes_are_mutually_exclusive() -> None:
    command = SandboxExecuteRequest(mode="command", command="printf ok")
    python = SandboxExecuteRequest(mode="python", script="print('ok')", args=["one"])
    shell = SandboxExecuteRequest(mode="shell", script="printf ok")

    assert command.mode == SandboxExecutionMode.COMMAND
    assert python.mode == SandboxExecutionMode.PYTHON
    assert shell.mode == SandboxExecutionMode.SHELL

    with pytest.raises(ValidationError, match="requires command"):
        SandboxExecuteRequest(mode="command", script="echo wrong")
    with pytest.raises(ValidationError, match="requires script"):
        SandboxExecuteRequest(mode="python", command="echo wrong")


def test_resource_limits_are_bounded() -> None:
    limits = SandboxLimits(timeout_seconds=300, cpu_cores=4, memory_mb=4096, pids_limit=256)
    assert limits.timeout_seconds == 300

    with pytest.raises(ValidationError):
        SandboxLimits(timeout_seconds=301)
    with pytest.raises(ValidationError):
        SandboxLimits(cpu_cores=0)
    with pytest.raises(ValidationError):
        SandboxLimits(memory_mb=32)
    with pytest.raises(ValidationError):
        SandboxLimits(pids_limit=1024)
