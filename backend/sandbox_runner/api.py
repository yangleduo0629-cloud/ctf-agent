from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from backend.db.enums import CallStatus
from backend.ingestion.api import ArtifactResponse
from backend.orchestration.event_store import IdempotencyConflictError
from backend.orchestration.redis_transport import LockUnavailableError
from backend.sandbox_runner.client import RunnerUnavailableError
from backend.sandbox_runner.contracts import SandboxExecuteRequest, SandboxExecutionMode
from backend.sandbox_runner.services import SandboxExecutionResult, SandboxExecutionService

router = APIRouter(prefix="/api", tags=["sandbox-runner"])


@dataclass(frozen=True)
class SandboxApiRuntime:
    executions: SandboxExecutionService


class SandboxOutputResponse(BaseModel):
    relative_path: str
    artifact: ArtifactResponse
    duplicate: bool


class SandboxExecutionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tool_call_id: uuid.UUID
    solver_run_id: uuid.UUID
    challenge_id: uuid.UUID
    sequence: int
    mode: SandboxExecutionMode
    status: CallStatus
    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    duration_ms: int
    archive_error: str | None
    outputs: list[SandboxOutputResponse]
    duplicate: bool


def _runtime(request: Request) -> SandboxApiRuntime:
    return request.app.state.sandbox


@router.post(
    "/solver-runs/{solver_run_id}/sandbox/executions",
    response_model=SandboxExecutionResponse,
)
async def execute_in_sandbox(
    solver_run_id: uuid.UUID,
    body: SandboxExecuteRequest,
    request: Request,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ],
) -> SandboxExecutionResponse:
    try:
        result = await _runtime(request).executions.execute(
            solver_run_id,
            body,
            idempotency_key=idempotency_key,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (IdempotencyConflictError, LockUnavailableError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RunnerUnavailableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _response(result)


def _response(result: SandboxExecutionResult) -> SandboxExecutionResponse:
    mode = SandboxExecutionMode(str(result.tool_call.arguments["mode"]))
    return SandboxExecutionResponse(
        tool_call_id=result.tool_call.id,
        solver_run_id=result.tool_call.solver_run_id,
        challenge_id=result.challenge_id,
        sequence=result.tool_call.sequence,
        mode=mode,
        status=result.tool_call.status,
        exit_code=result.runner_result.exit_code,
        stdout=result.runner_result.stdout,
        stderr=result.runner_result.stderr,
        stdout_truncated=result.runner_result.stdout_truncated,
        stderr_truncated=result.runner_result.stderr_truncated,
        timed_out=result.runner_result.timed_out,
        duration_ms=result.runner_result.duration_ms,
        archive_error=result.runner_result.archive_error,
        outputs=[
            SandboxOutputResponse(
                relative_path=output.relative_path,
                artifact=ArtifactResponse.model_validate(output.artifact),
                duplicate=output.duplicate,
            )
            for output in result.outputs
        ],
        duplicate=result.duplicate,
    )
