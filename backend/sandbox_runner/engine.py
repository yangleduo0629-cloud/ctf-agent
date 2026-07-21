from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Any

import aiodocker

from backend.ingestion.storage import safe_filename
from backend.sandbox_runner.contracts import (
    GeneratedFile,
    RunnerExecutionRequest,
    RunnerExecutionResponse,
    SandboxExecutionMode,
)

SANDBOX_LABEL = "ctf-platform.sandbox"
CONTAINER_UID = 65532
CONTAINER_GID = 65532


class ArchiveLimitError(ValueError):
    pass


class DockerSandboxRunner:
    def __init__(
        self,
        *,
        image: str,
        artifacts_root: Path,
        work_root: Path,
        max_output_bytes: int = 1024 * 1024,
        max_archive_bytes: int = 256 * 1024 * 1024,
        max_archive_files: int = 100,
    ) -> None:
        self.image = image
        self.artifacts_root = artifacts_root.resolve()
        self.work_root = work_root.resolve()
        self.max_output_bytes = max_output_bytes
        self.max_archive_bytes = max_archive_bytes
        self.max_archive_files = max_archive_files
        self.work_root.mkdir(parents=True, exist_ok=True)

    async def healthcheck(self) -> dict[str, str]:
        docker = aiodocker.Docker()
        try:
            version = await docker.version()
            image = await docker.images.inspect(self.image)
            return {
                "docker_version": str(version.get("Version", "unknown")),
                "image_id": str(image.get("Id", "unknown")),
            }
        finally:
            await docker.close()

    async def execute(self, request: RunnerExecutionRequest) -> RunnerExecutionResponse:
        try:
            _execution_root, attachments_dir, workspace_dir = await asyncio.to_thread(
                self._prepare_execution,
                request,
            )
        except Exception:
            await asyncio.to_thread(
                self.cleanup_execution,
                request.challenge_id,
                request.execution_id,
            )
            raise
        container_name = f"ctf-sandbox-{request.execution_id}"
        command = self._command(request, workspace_dir)
        config = self.container_config(request, attachments_dir, workspace_dir, command)
        docker = aiodocker.Docker()
        container = None
        started = time.monotonic()
        timed_out = False
        exit_code = -1
        stdout = ""
        stderr = ""
        container_id = ""
        archive_error: str | None = None
        generated_files: list[GeneratedFile] = []
        try:
            await self._remove_stale_container(docker, container_name)
            container = await docker.containers.create(config, name=container_name)
            container_id = container.id
            await container.start()
            try:
                wait_result = await asyncio.wait_for(
                    container.wait(),
                    timeout=request.limits.timeout_seconds,
                )
                exit_code = int(wait_result.get("StatusCode", -1))
            except TimeoutError:
                timed_out = True
                exit_code = 124
                await container.kill()
                await container.wait()

            stdout = "".join(await container.log(stdout=True, stderr=False))
            stderr = "".join(await container.log(stdout=False, stderr=True))
            await asyncio.to_thread(self._remove_internal_files, workspace_dir)
            try:
                generated_files = await asyncio.to_thread(
                    self.collect_generated_files,
                    workspace_dir,
                )
            except ArchiveLimitError as exc:
                archive_error = str(exc)
        finally:
            if container is not None:
                try:
                    await container.delete(force=True, v=True)
                except Exception:
                    pass
            await docker.close()

        stdout, stdout_truncated = _truncate_utf8(stdout, self.max_output_bytes)
        stderr, stderr_truncated = _truncate_utf8(stderr, self.max_output_bytes)
        return RunnerExecutionResponse(
            execution_id=request.execution_id,
            container_id=container_id[:12],
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            timed_out=timed_out,
            duration_ms=round((time.monotonic() - started) * 1000),
            generated_files=generated_files,
            archive_error=archive_error,
        )

    def execution_root(self, challenge_id, execution_id) -> Path:
        target = (self.work_root / str(challenge_id) / str(execution_id)).resolve()
        if not target.is_relative_to(self.work_root):
            raise ValueError("sandbox execution path escaped the work root")
        return target

    def container_config(
        self,
        request: RunnerExecutionRequest,
        attachments_dir: Path,
        workspace_dir: Path,
        command: list[str],
    ) -> dict[str, Any]:
        memory_bytes = request.limits.memory_mb * 1024 * 1024
        return {
            "Image": self.image,
            "Cmd": command,
            "WorkingDir": "/workspace",
            "User": f"{CONTAINER_UID}:{CONTAINER_GID}",
            "Env": ["HOME=/tmp", "PYTHONDONTWRITEBYTECODE=1"],
            "Tty": False,
            "OpenStdin": False,
            "Labels": {
                SANDBOX_LABEL: "true",
                "ctf-platform.challenge-id": str(request.challenge_id),
                "ctf-platform.execution-id": str(request.execution_id),
            },
            "HostConfig": {
                "Binds": [
                    f"{attachments_dir}:/attachments:ro",
                    f"{workspace_dir}:/workspace:rw",
                ],
                "NetworkMode": "none",
                "ReadonlyRootfs": True,
                "Privileged": False,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "Memory": memory_bytes,
                "MemorySwap": memory_bytes,
                "NanoCpus": int(request.limits.cpu_cores * 1_000_000_000),
                "PidsLimit": request.limits.pids_limit,
                "OomKillDisable": False,
                "LogConfig": {
                    "Type": "local",
                    "Config": {"max-size": "2m", "max-file": "2"},
                },
                "Ulimits": [
                    {"Name": "nofile", "Soft": 1024, "Hard": 1024},
                    {"Name": "core", "Soft": 0, "Hard": 0},
                ],
                "Tmpfs": {
                    "/tmp": "rw,noexec,nosuid,nodev,size=64m,mode=1777",
                    "/run": "rw,noexec,nosuid,nodev,size=16m,mode=755",
                },
            },
        }

    def collect_generated_files(self, workspace_dir: Path) -> list[GeneratedFile]:
        workspace = workspace_dir.resolve()
        files: list[GeneratedFile] = []
        total_size = 0
        for path in sorted(workspace.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(workspace):
                continue
            relative_path = path.relative_to(workspace).as_posix()
            size = path.stat().st_size
            total_size += size
            if len(files) >= self.max_archive_files:
                raise ArchiveLimitError(
                    f"generated file count exceeds {self.max_archive_files} file limit"
                )
            if total_size > self.max_archive_bytes:
                raise ArchiveLimitError(
                    f"generated files exceed {self.max_archive_bytes} byte limit"
                )
            files.append(GeneratedFile(relative_path=relative_path, size_bytes=size))
        return files

    def cleanup_execution(self, challenge_id, execution_id) -> None:
        _remove_tree(self.execution_root(challenge_id, execution_id))

    def _prepare_execution(
        self,
        request: RunnerExecutionRequest,
    ) -> tuple[Path, Path, Path]:
        execution_root = self.execution_root(request.challenge_id, request.execution_id)
        _remove_tree(execution_root)
        challenge_root = execution_root.parent
        challenge_root.mkdir(parents=True, exist_ok=True)
        _set_directory_access(challenge_root, 0o770)
        execution_root.mkdir()
        attachments_dir = execution_root / "attachments"
        workspace_dir = execution_root / "workspace"
        attachments_dir.mkdir()
        workspace_dir.mkdir()
        _set_directory_access(execution_root, 0o770)
        _set_directory_access(attachments_dir, 0o770)
        _set_directory_access(workspace_dir, 0o770)

        used_names: set[str] = set()
        for attachment in request.attachments:
            source = (self.artifacts_root / attachment.storage_key).resolve()
            if not source.is_relative_to(self.artifacts_root) or not source.is_file():
                raise FileNotFoundError(f"artifact storage key was not found: {attachment.storage_key}")
            name = safe_filename(attachment.original_name)
            if name in used_names:
                name = f"{str(attachment.artifact_id)[:8]}-{name}"
            used_names.add(name)
            destination = attachments_dir / name
            shutil.copyfile(source, destination)
            destination.chmod(0o640)
            _set_owner(destination)
        return execution_root, attachments_dir, workspace_dir

    @staticmethod
    def _command(request: RunnerExecutionRequest, workspace_dir: Path) -> list[str]:
        if request.mode == SandboxExecutionMode.COMMAND:
            return ["/bin/sh", "-c", request.command or ""]

        internal_dir = workspace_dir / ".runner"
        internal_dir.mkdir()
        _set_directory_access(internal_dir, 0o700)
        suffix = "py" if request.mode == SandboxExecutionMode.PYTHON else "sh"
        script_path = internal_dir / f"task.{suffix}"
        script_path.write_text(request.script or "", encoding="utf-8", newline="\n")
        script_path.chmod(0o500)
        _set_owner(script_path)
        container_path = f"/workspace/.runner/task.{suffix}"
        interpreter = "/usr/local/bin/python" if suffix == "py" else "/bin/sh"
        return [interpreter, container_path, *request.args]

    @staticmethod
    def _remove_internal_files(workspace_dir: Path) -> None:
        _remove_tree(workspace_dir / ".runner")

    @staticmethod
    async def _remove_stale_container(docker, name: str) -> None:
        try:
            stale = await docker.containers.get(name)
        except aiodocker.exceptions.DockerError as exc:
            if exc.status == 404:
                return
            raise
        await stale.delete(force=True, v=True)


def _set_directory_access(path: Path, mode: int) -> None:
    path.chmod(mode)
    _set_owner(path)


def _set_owner(path: Path) -> None:
    if os.name != "nt":
        os.chown(path, CONTAINER_UID, CONTAINER_GID)


def _truncate_utf8(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return

    def make_writable(function, target, _error) -> None:
        Path(target).chmod(0o700)
        function(target)

    shutil.rmtree(path, onexc=make_writable)
