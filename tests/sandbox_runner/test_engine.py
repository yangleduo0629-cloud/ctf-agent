import os
import uuid
from pathlib import Path

import pytest

from backend.sandbox_runner.contracts import (
    RunnerAttachment,
    RunnerExecutionRequest,
    SandboxExecutionMode,
    SandboxLimits,
)
from backend.sandbox_runner.engine import (
    ArchiveLimitError,
    DockerSandboxRunner,
    _applied_config,
)


def test_container_config_enforces_isolation_and_limits(tmp_path: Path) -> None:
    runner = DockerSandboxRunner(
        image="ctf-sandbox-runner:local",
        artifacts_root=tmp_path / "artifacts",
        work_root=tmp_path / "sandboxes",
    )
    request = RunnerExecutionRequest(
        execution_id=uuid.uuid4(),
        challenge_id=uuid.uuid4(),
        mode="command",
        command="printf ok",
        limits=SandboxLimits(
            timeout_seconds=10,
            cpu_cores=1.5,
            memory_mb=256,
            pids_limit=32,
        ),
    )
    attachments = tmp_path / "attachments"
    workspace = tmp_path / "workspace"
    config = runner.container_config(
        request,
        attachments,
        workspace,
        ["/bin/sh", "-c", "printf ok"],
    )

    host = config["HostConfig"]
    assert config["User"] == "65532:65532"
    assert host["NetworkMode"] == "none"
    assert host["ReadonlyRootfs"] is True
    assert host["Privileged"] is False
    assert host["CapDrop"] == ["ALL"]
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["Memory"] == host["MemorySwap"] == 256 * 1024 * 1024
    assert host["NanoCpus"] == 1_500_000_000
    assert host["PidsLimit"] == 32
    assert host["LogConfig"]["Config"] == {"max-size": "2m", "max-file": "2"}
    assert f"{attachments}:/attachments:ro" in host["Binds"]
    assert f"{workspace}:/workspace:rw" in host["Binds"]


def test_prepare_execution_isolates_read_only_attachments_and_script(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    challenge_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    storage_key = f"{challenge_id}/input.txt"
    source = artifact_root / storage_key
    source.parent.mkdir()
    source.write_text("original", encoding="utf-8")
    runner = DockerSandboxRunner(
        image="fixture",
        artifacts_root=artifact_root,
        work_root=tmp_path / "sandboxes",
    )
    request = RunnerExecutionRequest(
        execution_id=uuid.uuid4(),
        challenge_id=challenge_id,
        mode=SandboxExecutionMode.PYTHON,
        script="print('ok')",
        attachments=[
            RunnerAttachment(
                artifact_id=artifact_id,
                storage_key=storage_key,
                original_name="input.txt",
            )
        ],
    )

    _root, attachments, workspace = runner._prepare_execution(request)
    command = runner._command(request, workspace)

    mounted = attachments / "input.txt"
    assert mounted.read_text(encoding="utf-8") == "original"
    assert command[:2] == ["/usr/local/bin/python", "/workspace/.runner/task.py"]
    assert (workspace / ".runner" / "task.py").read_text(encoding="utf-8") == "print('ok')"

    runner.cleanup_execution(request.challenge_id, request.execution_id)
    assert not workspace.parent.exists()


def test_generated_file_collection_skips_symlinks_and_enforces_limits(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "result.txt").write_text("result", encoding="utf-8")
    nested = workspace / "nested"
    nested.mkdir()
    (nested / "data.bin").write_bytes(b"12")
    if os.name != "nt":
        (workspace / "outside-link").symlink_to(tmp_path / "outside")

    runner = DockerSandboxRunner(
        image="fixture",
        artifacts_root=tmp_path / "artifacts",
        work_root=tmp_path / "sandboxes",
        max_archive_bytes=8,
        max_archive_files=2,
    )
    files = runner.collect_generated_files(workspace)
    assert [(item.relative_path, item.size_bytes) for item in files] == [
        ("nested/data.bin", 2),
        ("result.txt", 6),
    ]

    (workspace / "third.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ArchiveLimitError, match="file limit"):
        runner.collect_generated_files(workspace)


def test_applied_config_is_derived_from_docker_inspect() -> None:
    applied = _applied_config(
        {
            "HostConfig": {
                "Memory": 134217728,
                "NanoCpus": 500000000,
                "PidsLimit": 32,
                "NetworkMode": "none",
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
            },
            "Mounts": [
                {"Destination": "/attachments", "RW": False},
                {"Destination": "/workspace", "RW": True},
            ],
        }
    )
    assert applied.memory_bytes == 134217728
    assert applied.nano_cpus == 500000000
    assert applied.pids_limit == 32
    assert applied.network_mode == "none"
    assert applied.readonly_rootfs
    assert applied.cap_drop == ["ALL"]
    assert applied.no_new_privileges
    assert applied.attachments_read_only
    assert applied.workspace_read_write
