from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.db.base import utc_now
from backend.db.enums import CallStatus, EvidenceKind
from backend.db.models import Artifact, Evidence, SolverRun, ToolCall
from backend.db.session import session_scope
from backend.ingestion.services import ArtifactIngestionResult, ChallengeCatalog
from backend.orchestration.event_store import EventStore
from backend.orchestration.redis_transport import RedisLeaseLock
from backend.sandbox_runner.client import RunnerHttpClient
from backend.sandbox_runner.contracts import (
    RunnerAttachment,
    RunnerExecutionRequest,
    RunnerExecutionResponse,
    SandboxExecuteRequest,
)


@dataclass(frozen=True)
class ArchivedSandboxOutput:
    relative_path: str
    artifact: Artifact
    duplicate: bool


@dataclass(frozen=True)
class SandboxExecutionResult:
    tool_call: ToolCall
    challenge_id: uuid.UUID
    runner_result: RunnerExecutionResponse
    outputs: tuple[ArchivedSandboxOutput, ...]
    duplicate: bool


@dataclass(frozen=True)
class PreparedExecution:
    tool_call_id: uuid.UUID
    challenge_id: uuid.UUID
    attachments: tuple[RunnerAttachment, ...]
    completed: bool


class SandboxExecutionService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        catalog: ChallengeCatalog,
        runner: RunnerHttpClient,
        redis: Redis,
        work_root: Path,
    ) -> None:
        self.session_factory = session_factory
        self.catalog = catalog
        self.runner = runner
        self.redis = redis
        self.work_root = work_root.resolve()

    async def execute(
        self,
        solver_run_id: uuid.UUID,
        request: SandboxExecuteRequest,
        *,
        idempotency_key: str,
    ) -> SandboxExecutionResult:
        request_hash = _request_hash(request)
        prepared = await asyncio.to_thread(
            self._prepare,
            solver_run_id,
            request,
            request_hash,
            idempotency_key,
        )
        if prepared.completed:
            return await asyncio.to_thread(self._load_result, prepared.tool_call_id, True)

        lock = RedisLeaseLock(
            self.redis,
            f"sandbox-execution:{prepared.tool_call_id}",
            lease_ms=(request.limits.timeout_seconds + 60) * 1000,
            acquire_timeout=2.0,
        )
        async with lock:
            replay = await asyncio.to_thread(self._load_if_completed, prepared.tool_call_id)
            if replay is not None:
                return replay
            runner_request = RunnerExecutionRequest(
                execution_id=prepared.tool_call_id,
                challenge_id=prepared.challenge_id,
                attachments=list(prepared.attachments),
                **request.model_dump(),
            )
            try:
                runner_result = await self.runner.execute(runner_request)
                outputs = await self._archive_outputs(
                    prepared.challenge_id,
                    prepared.tool_call_id,
                    runner_result,
                    idempotency_key,
                )
                return await asyncio.to_thread(
                    self._complete,
                    prepared.tool_call_id,
                    runner_result,
                    outputs,
                    idempotency_key,
                )
            except Exception as exc:
                await asyncio.to_thread(
                    self._fail,
                    prepared.tool_call_id,
                    str(exc),
                    idempotency_key,
                )
                raise
            finally:
                await asyncio.to_thread(
                    self.cleanup_execution,
                    prepared.challenge_id,
                    prepared.tool_call_id,
                )

    def cleanup_execution(self, challenge_id: uuid.UUID, execution_id: uuid.UUID) -> None:
        target = (self.work_root / str(challenge_id) / str(execution_id)).resolve()
        if not target.is_relative_to(self.work_root):
            raise ValueError("sandbox cleanup path escaped the work root")
        shutil.rmtree(target, ignore_errors=True)

    def _prepare(
        self,
        solver_run_id: uuid.UUID,
        request: SandboxExecuteRequest,
        request_hash: str,
        idempotency_key: str,
    ) -> PreparedExecution:
        with session_scope(self.session_factory) as session:
            solver_run = session.scalar(
                select(SolverRun)
                .where(SolverRun.id == solver_run_id, SolverRun.deleted_at.is_(None))
                .with_for_update()
            )
            if solver_run is None:
                raise LookupError(f"SolverRun not found: {solver_run_id}")
            events = EventStore(session)
            existing = events.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                events.require_matching(
                    existing,
                    aggregate_type="tool_call",
                    aggregate_id=existing.aggregate_id,
                    event_type="sandbox.execution.started",
                    payload_values={
                        "solver_run_id": str(solver_run_id),
                        "request_sha256": request_hash,
                    },
                )
                tool_call = session.get(ToolCall, existing.aggregate_id)
                if tool_call is None:
                    raise LookupError(f"ToolCall not found: {existing.aggregate_id}")
                attachments = self._attachments_from_arguments(session, tool_call.arguments)
                return PreparedExecution(
                    tool_call.id,
                    solver_run.challenge_id,
                    tuple(attachments),
                    tool_call.status != CallStatus.STARTED,
                )

            artifacts = list(
                session.scalars(
                    select(Artifact).where(
                        Artifact.challenge_id == solver_run.challenge_id,
                        Artifact.deleted_at.is_(None),
                        Artifact.purged_at.is_(None),
                    )
                )
            )
            original_artifacts = [
                artifact
                for artifact in artifacts
                if artifact.details.get("kind") != "sandbox-output"
            ]
            sequence = session.scalar(
                select(func.max(ToolCall.sequence)).where(
                    ToolCall.solver_run_id == solver_run_id,
                    ToolCall.deleted_at.is_(None),
                )
            )
            attachments = [_runner_attachment(artifact) for artifact in original_artifacts]
            tool_call = ToolCall(
                solver_run_id=solver_run_id,
                sequence=(sequence if sequence is not None else -1) + 1,
                tool_name=f"sandbox.{request.mode.value}",
                status=CallStatus.STARTED,
                arguments={
                    "mode": request.mode.value,
                    "command": request.command,
                    "script_sha256": (
                        hashlib.sha256(request.script.encode()).hexdigest()
                        if request.script is not None
                        else None
                    ),
                    "args": request.args,
                    "limits": request.limits.model_dump(),
                    "attachment_ids": [str(item.artifact_id) for item in attachments],
                    "request_sha256": request_hash,
                },
            )
            session.add(tool_call)
            session.flush()
            events.append(
                aggregate_type="tool_call",
                aggregate_id=tool_call.id,
                aggregate_version=tool_call.version,
                event_type="sandbox.execution.started",
                payload={
                    "solver_run_id": str(solver_run_id),
                    "challenge_id": str(solver_run.challenge_id),
                    "request_sha256": request_hash,
                },
                idempotency_key=idempotency_key,
            )
            return PreparedExecution(
                tool_call.id,
                solver_run.challenge_id,
                tuple(attachments),
                False,
            )

    def _attachments_from_arguments(
        self,
        session: Session,
        arguments: dict[str, Any],
    ) -> list[RunnerAttachment]:
        artifact_ids = [uuid.UUID(value) for value in arguments.get("attachment_ids", [])]
        artifacts = [session.get(Artifact, artifact_id) for artifact_id in artifact_ids]
        if any(artifact is None for artifact in artifacts):
            raise LookupError("Sandbox input artifact was not found")
        return [_runner_attachment(artifact) for artifact in artifacts if artifact is not None]

    async def _archive_outputs(
        self,
        challenge_id: uuid.UUID,
        tool_call_id: uuid.UUID,
        result: RunnerExecutionResponse,
        idempotency_key: str,
    ) -> tuple[ArchivedSandboxOutput, ...]:
        workspace = (self.work_root / str(challenge_id) / str(tool_call_id) / "workspace").resolve()
        if not workspace.is_relative_to(self.work_root):
            raise ValueError("sandbox workspace path escaped the work root")
        archived: list[ArchivedSandboxOutput] = []
        for generated in result.generated_files:
            candidate = workspace / generated.relative_path
            path = candidate.resolve()
            if candidate.is_symlink() or not path.is_relative_to(workspace) or not path.is_file():
                raise ValueError(f"invalid generated file path: {generated.relative_path}")
            ingestion: ArtifactIngestionResult = await asyncio.to_thread(
                self.catalog.add_artifact_file,
                challenge_id,
                path,
                idempotency_key=_derived_key(
                    idempotency_key,
                    f"artifact:{hashlib.sha256(generated.relative_path.encode()).hexdigest()[:16]}",
                ),
                source_url=f"sandbox://{tool_call_id}/{quote(generated.relative_path)}",
                details={
                    "kind": "sandbox-output",
                    "tool_call_id": str(tool_call_id),
                    "relative_path": generated.relative_path,
                },
            )
            archived.append(
                ArchivedSandboxOutput(
                    generated.relative_path,
                    ingestion.artifact,
                    ingestion.duplicate,
                )
            )
        return tuple(archived)

    def _complete(
        self,
        tool_call_id: uuid.UUID,
        runner_result: RunnerExecutionResponse,
        outputs: tuple[ArchivedSandboxOutput, ...],
        idempotency_key: str,
    ) -> SandboxExecutionResult:
        with session_scope(self.session_factory) as session:
            tool_call = session.scalar(
                select(ToolCall).where(ToolCall.id == tool_call_id).with_for_update()
            )
            if tool_call is None:
                raise LookupError(f"ToolCall not found: {tool_call_id}")
            if tool_call.status != CallStatus.STARTED:
                return self._load_result_in_session(session, tool_call, True)

            failed = (
                runner_result.exit_code != 0
                or runner_result.timed_out
                or runner_result.archive_error is not None
            )
            tool_call.status = CallStatus.FAILED if failed else CallStatus.SUCCEEDED
            tool_call.completed_at = utc_now()
            tool_call.error = _execution_error(runner_result)
            tool_call.result = {
                **runner_result.model_dump(mode="json"),
                "outputs": [
                    {
                        "relative_path": output.relative_path,
                        "artifact_id": str(output.artifact.id),
                        "duplicate": output.duplicate,
                    }
                    for output in outputs
                ],
            }
            session.add(
                Evidence(
                    challenge_id=outputs[0].artifact.challenge_id if outputs else self._challenge_id(session, tool_call),
                    solver_run_id=tool_call.solver_run_id,
                    tool_call_id=tool_call.id,
                    kind=EvidenceKind.LOG,
                    title=f"Sandbox {tool_call.sequence} output",
                    content=_log_content(runner_result.stdout, runner_result.stderr),
                    details={
                        "exit_code": runner_result.exit_code,
                        "timed_out": runner_result.timed_out,
                        "duration_ms": runner_result.duration_ms,
                        "stdout_truncated": runner_result.stdout_truncated,
                        "stderr_truncated": runner_result.stderr_truncated,
                    },
                )
            )
            challenge_id = self._challenge_id(session, tool_call)
            for output in outputs:
                session.add(
                    Evidence(
                        challenge_id=challenge_id,
                        solver_run_id=tool_call.solver_run_id,
                        tool_call_id=tool_call.id,
                        artifact_id=output.artifact.id,
                        kind=EvidenceKind.FILE,
                        title=output.relative_path[:255],
                        details={"relative_path": output.relative_path},
                    )
                )
            session.flush()
            EventStore(session).append(
                aggregate_type="tool_call",
                aggregate_id=tool_call.id,
                aggregate_version=tool_call.version,
                event_type="sandbox.execution.completed",
                payload={
                    "status": tool_call.status.value,
                    "exit_code": runner_result.exit_code,
                    "timed_out": runner_result.timed_out,
                    "artifact_ids": [str(output.artifact.id) for output in outputs],
                },
                idempotency_key=_derived_key(idempotency_key, "completed"),
            )
            return SandboxExecutionResult(
                tool_call,
                challenge_id,
                runner_result,
                outputs,
                False,
            )

    def _fail(
        self,
        tool_call_id: uuid.UUID,
        error: str,
        idempotency_key: str,
    ) -> None:
        with session_scope(self.session_factory) as session:
            tool_call = session.scalar(
                select(ToolCall).where(ToolCall.id == tool_call_id).with_for_update()
            )
            if tool_call is None or tool_call.status != CallStatus.STARTED:
                return
            tool_call.status = CallStatus.FAILED
            tool_call.completed_at = utc_now()
            tool_call.error = error[:4000]
            tool_call.result = {"transport_error": error[:4000], "outputs": []}
            session.flush()
            EventStore(session).append(
                aggregate_type="tool_call",
                aggregate_id=tool_call.id,
                aggregate_version=tool_call.version,
                event_type="sandbox.execution.completed",
                payload={"status": CallStatus.FAILED.value, "transport_error": True},
                idempotency_key=_derived_key(idempotency_key, "completed"),
            )

    def _load_if_completed(self, tool_call_id: uuid.UUID) -> SandboxExecutionResult | None:
        with self.session_factory() as session:
            tool_call = session.get(ToolCall, tool_call_id)
            if tool_call is None:
                raise LookupError(f"ToolCall not found: {tool_call_id}")
            if tool_call.status == CallStatus.STARTED:
                return None
            return self._load_result_in_session(session, tool_call, True)

    def _load_result(self, tool_call_id: uuid.UUID, duplicate: bool) -> SandboxExecutionResult:
        with self.session_factory() as session:
            tool_call = session.get(ToolCall, tool_call_id)
            if tool_call is None:
                raise LookupError(f"ToolCall not found: {tool_call_id}")
            return self._load_result_in_session(session, tool_call, duplicate)

    def _load_result_in_session(
        self,
        session: Session,
        tool_call: ToolCall,
        duplicate: bool,
    ) -> SandboxExecutionResult:
        raw = dict(tool_call.result)
        outputs: list[ArchivedSandboxOutput] = []
        for item in raw.pop("outputs", []):
            artifact = session.get(Artifact, uuid.UUID(str(item["artifact_id"])))
            if artifact is None:
                raise LookupError(f"Artifact not found: {item['artifact_id']}")
            outputs.append(
                ArchivedSandboxOutput(
                    str(item["relative_path"]),
                    artifact,
                    bool(item["duplicate"]),
                )
            )
        if "execution_id" not in raw:
            raw = {
                "execution_id": tool_call.id,
                "container_id": "",
                "exit_code": -1,
                "stdout": "",
                "stderr": tool_call.error or "sandbox runner failed",
                "stdout_truncated": False,
                "stderr_truncated": False,
                "timed_out": False,
                "duration_ms": 0,
                "generated_files": [],
                "archive_error": tool_call.error,
            }
        return SandboxExecutionResult(
            tool_call,
            self._challenge_id(session, tool_call),
            RunnerExecutionResponse.model_validate(raw),
            tuple(outputs),
            duplicate,
        )

    @staticmethod
    def _challenge_id(session: Session, tool_call: ToolCall) -> uuid.UUID:
        solver_run = session.get(SolverRun, tool_call.solver_run_id)
        if solver_run is None:
            raise LookupError(f"SolverRun not found: {tool_call.solver_run_id}")
        return solver_run.challenge_id


def _runner_attachment(artifact: Artifact) -> RunnerAttachment:
    return RunnerAttachment(
        artifact_id=artifact.id,
        storage_key=artifact.storage_key,
        original_name=artifact.original_name,
    )


def _request_hash(request: SandboxExecuteRequest) -> str:
    encoded = json.dumps(
        request.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _derived_key(base: str, suffix: str) -> str:
    candidate = f"{base}:{suffix}"
    if len(candidate) <= 255:
        return candidate
    digest = hashlib.sha256(base.encode()).hexdigest()[:16]
    return f"{base[: 238 - len(suffix)]}:{digest}:{suffix}"


def _execution_error(result: RunnerExecutionResponse) -> str | None:
    if result.archive_error:
        return result.archive_error
    if result.timed_out:
        return f"command timed out after {result.duration_ms} ms"
    if result.exit_code != 0:
        return f"command exited with code {result.exit_code}"
    return None


def _log_content(stdout: str, stderr: str) -> str:
    return f"stdout:\n{stdout}\n\nstderr:\n{stderr}"
