from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request

from backend.sandbox_runner.contracts import RunnerExecutionRequest, RunnerExecutionResponse
from backend.sandbox_runner.engine import DockerSandboxRunner

router = APIRouter()


@dataclass(frozen=True)
class InternalRunnerRuntime:
    engine: DockerSandboxRunner
    token: str


def _runtime(request: Request) -> InternalRunnerRuntime:
    return request.app.state.runner


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, str]:
    status = await _runtime(request).engine.healthcheck()
    return {"status": "ok", **status}


@router.post("/internal/v1/execute", response_model=RunnerExecutionResponse)
async def execute(
    body: RunnerExecutionRequest,
    request: Request,
    runner_token: Annotated[str, Header(alias="X-Runner-Token")],
) -> RunnerExecutionResponse:
    runtime = _runtime(request)
    if not secrets.compare_digest(runner_token, runtime.token):
        raise HTTPException(status_code=401, detail="invalid runner token")
    return await runtime.engine.execute(body)
