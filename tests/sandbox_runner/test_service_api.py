from __future__ import annotations

import io
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.db.enums import CallStatus, EvidenceKind
from backend.db.models import Artifact, DomainEvent, Evidence, ToolCall
from backend.ingestion.services import ChallengeCatalog
from backend.sandbox_runner.api import SandboxApiRuntime, router
from backend.sandbox_runner.client import RunnerHttpClient
from backend.sandbox_runner.contracts import (
    GeneratedFile,
    RunnerExecutionRequest,
    RunnerExecutionResponse,
)
from backend.sandbox_runner.services import SandboxExecutionService


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, *, nx: bool, px: int) -> bool:
        del px
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script: str, numkeys: int, *values: str) -> int:
        del numkeys
        key, token = values[:2]
        if self.values.get(key) != token:
            return 0
        if "DEL" in script:
            del self.values[key]
        return 1


class FakeRunner:
    def __init__(self, work_root: Path) -> None:
        self.work_root = work_root
        self.requests: list[RunnerExecutionRequest] = []

    async def execute(self, request: RunnerExecutionRequest) -> RunnerExecutionResponse:
        self.requests.append(request)
        workspace = (
            self.work_root
            / str(request.challenge_id)
            / str(request.execution_id)
            / "workspace"
        )
        workspace.mkdir(parents=True)
        (workspace / "result.txt").write_text("generated", encoding="utf-8")
        return RunnerExecutionResponse(
            execution_id=request.execution_id,
            container_id="abcdef123456",
            exit_code=0,
            stdout="command output\n",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            duration_ms=25,
            generated_files=[GeneratedFile(relative_path="result.txt", size_bytes=9)],
        )


def test_public_api_archives_outputs_and_replays_idempotently(
    catalog: ChallengeCatalog,
    session_factory: sessionmaker[Session],
    sandbox_work_root: Path,
) -> None:
    competition = catalog.create_competition(
        name="Sandbox Fixture",
        slug="sandbox-fixture",
        platform="local",
        idempotency_key="sandbox:competition",
    )
    challenge = catalog.create_challenge(
        competition.id,
        name="Runner",
        slug="runner",
        category="misc",
        idempotency_key="sandbox:challenge",
    )
    input_artifact = catalog.add_artifact_fileobj(
        challenge.id,
        "input.txt",
        io.BytesIO(b"input"),
        idempotency_key="sandbox:input",
    ).artifact
    solver_run = catalog.create_solver_run(
        challenge.id,
        run_key="runner-1",
        solver_type="agent",
        model_spec="fixture",
        idempotency_key="sandbox:solver-run",
    )
    fake_runner = FakeRunner(sandbox_work_root)
    service = SandboxExecutionService(
        session_factory,
        catalog,
        cast(RunnerHttpClient, fake_runner),
        cast(Redis, FakeRedis()),
        sandbox_work_root,
    )
    app = FastAPI()
    app.state.sandbox = SandboxApiRuntime(service)
    app.include_router(router)

    with TestClient(app) as client:
        first = client.post(
            f"/api/solver-runs/{solver_run.id}/sandbox/executions",
            headers={"Idempotency-Key": "sandbox:execute:1"},
            json={"mode": "command", "command": "cat /attachments/input.txt"},
        )
        replay = client.post(
            f"/api/solver-runs/{solver_run.id}/sandbox/executions",
            headers={"Idempotency-Key": "sandbox:execute:1"},
            json={"mode": "command", "command": "cat /attachments/input.txt"},
        )

    assert first.status_code == replay.status_code == 200
    payload = first.json()
    assert payload["status"] == "succeeded"
    assert payload["stdout"] == "command output\n"
    assert payload["outputs"][0]["relative_path"] == "result.txt"
    assert payload["outputs"][0]["artifact"]["sha256"]
    assert replay.json()["duplicate"] is True
    assert len(fake_runner.requests) == 1
    assert fake_runner.requests[0].attachments[0].artifact_id == input_artifact.id
    assert not sandbox_work_root.joinpath(
        str(challenge.id),
        payload["tool_call_id"],
    ).exists()

    with session_factory() as session:
        tool_call = session.scalar(select(ToolCall))
        assert tool_call is not None
        assert tool_call.status == CallStatus.SUCCEEDED
        assert tool_call.result["exit_code"] == 0
        output = session.scalar(
            select(Artifact).where(Artifact.details["kind"].as_string() == "sandbox-output")
        )
        assert output is not None
        evidence = session.scalars(select(Evidence).where(Evidence.tool_call_id == tool_call.id)).all()
        assert {item.kind for item in evidence} == {EvidenceKind.LOG, EvidenceKind.FILE}
        assert session.scalar(
            select(func.count())
            .select_from(DomainEvent)
            .where(DomainEvent.event_type.like("sandbox.execution.%"))
        ) == 2


def test_api_rejects_idempotency_key_reuse_with_different_command(
    catalog: ChallengeCatalog,
    session_factory: sessionmaker[Session],
    sandbox_work_root: Path,
) -> None:
    competition = catalog.create_competition(
        name="Conflict",
        slug="conflict",
        platform="local",
        idempotency_key="conflict:competition",
    )
    challenge = catalog.create_challenge(
        competition.id,
        name="Conflict",
        slug="conflict",
        category="misc",
        idempotency_key="conflict:challenge",
    )
    solver_run = catalog.create_solver_run(
        challenge.id,
        run_key="conflict-1",
        solver_type="agent",
        model_spec="fixture",
        idempotency_key="conflict:run",
    )
    service = SandboxExecutionService(
        session_factory,
        catalog,
        cast(RunnerHttpClient, FakeRunner(sandbox_work_root)),
        cast(Redis, FakeRedis()),
        sandbox_work_root,
    )
    app = FastAPI()
    app.state.sandbox = SandboxApiRuntime(service)
    app.include_router(router)

    with TestClient(app) as client:
        first = client.post(
            f"/api/solver-runs/{solver_run.id}/sandbox/executions",
            headers={"Idempotency-Key": "conflict:execute"},
            json={"mode": "command", "command": "printf first"},
        )
        conflict = client.post(
            f"/api/solver-runs/{solver_run.id}/sandbox/executions",
            headers={"Idempotency-Key": "conflict:execute"},
            json={"mode": "command", "command": "printf second"},
        )

    assert first.status_code == 200
    assert conflict.status_code == 409
