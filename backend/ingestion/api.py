from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from backend.db.enums import ChallengeStatus, SolverRunStatus
from backend.db.models import Challenge, Competition, SolverRun
from backend.ingestion.ctfd import CTFdConnectionConfig, CTFdSourceClient, CTFdSynchronizer
from backend.ingestion.services import ArtifactIngestionResult, ChallengeCatalog
from backend.orchestration.event_store import IdempotencyConflictError

router = APIRouter(prefix="/api", tags=["challenge-ingestion"])


@dataclass(frozen=True)
class IngestionRuntime:
    catalog: ChallengeCatalog


class CompetitionCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9-]*$")
    platform: str = Field(default="local", min_length=1, max_length=64)
    external_id: str | None = Field(default=None, max_length=128)
    flag_format: str = Field(default="flag{...}", min_length=1, max_length=128)
    flag_regex: str = Field(default=r"^flag\{[^}\r\n]+\}$", min_length=1, max_length=512)
    details: dict[str, Any] = Field(default_factory=dict)


class CompetitionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    platform: str
    external_id: str | None
    flag_format: str
    flag_regex: str
    version: int


class ChallengeCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9-]*$")
    category: str = Field(default="", max_length=128)
    description: str = ""
    points: int = Field(default=0, ge=0)
    external_id: str | None = Field(default=None, max_length=128)
    connection_info: str | None = None
    service_protocol: str | None = Field(default=None, max_length=32)
    service_host: str | None = Field(default=None, max_length=255)
    service_port: int | None = Field(default=None, ge=1, le=65535)
    details: dict[str, Any] = Field(default_factory=dict)


class ChallengeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    competition_id: uuid.UUID
    external_id: str | None
    slug: str
    name: str
    category: str
    description: str
    points: int
    connection_info: str | None
    service_protocol: str | None
    service_host: str | None
    service_port: int | None
    status: ChallengeStatus
    version: int


class ArtifactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    challenge_id: uuid.UUID
    original_name: str
    storage_key: str
    content_type: str | None
    size_bytes: int
    sha256: str
    source_url: str | None
    version: int


class ArtifactUploadResponse(BaseModel):
    artifact: ArtifactResponse
    duplicate: bool


class SolverRunCreateRequest(BaseModel):
    run_key: str = Field(min_length=1, max_length=128)
    solver_type: str = Field(min_length=1, max_length=64)
    model_spec: str = Field(min_length=1, max_length=255)
    attempt: int = Field(default=1, ge=1)


class SolverRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    challenge_id: uuid.UUID
    run_key: str
    attempt: int
    solver_type: str
    model_spec: str
    status: SolverRunStatus
    version: int


class CTFdSyncRequest(BaseModel):
    download_files: bool = True


class CTFdSyncResponse(BaseModel):
    challenges_seen: int
    challenges_synced: int
    artifacts_created: int
    artifacts_reused: int


def _runtime(request: Request) -> IngestionRuntime:
    return request.app.state.ingestion


@router.post("/competitions", response_model=CompetitionResponse, status_code=201)
async def create_competition(
    body: CompetitionCreateRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> Competition:
    return await _catalog_call(
        _runtime(request).catalog.create_competition,
        **body.model_dump(),
        idempotency_key=idempotency_key,
    )


@router.post(
    "/competitions/{competition_id}/challenges",
    response_model=ChallengeResponse,
    status_code=201,
)
async def create_challenge(
    competition_id: uuid.UUID,
    body: ChallengeCreateRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> Challenge:
    return await _catalog_call(
        _runtime(request).catalog.create_challenge,
        competition_id,
        **body.model_dump(),
        idempotency_key=idempotency_key,
    )


@router.post(
    "/challenges/{challenge_id}/artifacts",
    response_model=ArtifactUploadResponse,
    status_code=201,
)
async def upload_artifact(
    challenge_id: uuid.UUID,
    file: UploadFile,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> ArtifactUploadResponse:
    result: ArtifactIngestionResult = await _catalog_call(
        _runtime(request).catalog.add_artifact_fileobj,
        challenge_id,
        file.filename or "artifact.bin",
        file.file,
        idempotency_key=idempotency_key,
        declared_content_type=file.content_type,
    )
    return ArtifactUploadResponse(
        artifact=ArtifactResponse.model_validate(result.artifact),
        duplicate=result.duplicate,
    )


@router.post(
    "/challenges/{challenge_id}/solver-runs",
    response_model=SolverRunResponse,
    status_code=201,
)
async def create_solver_run(
    challenge_id: uuid.UUID,
    body: SolverRunCreateRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> SolverRun:
    return await _catalog_call(
        _runtime(request).catalog.create_solver_run,
        challenge_id,
        **body.model_dump(),
        idempotency_key=idempotency_key,
    )


@router.post(
    "/competitions/{competition_id}/ctfd/sync",
    response_model=CTFdSyncResponse,
)
async def sync_ctfd(
    competition_id: uuid.UUID,
    body: CTFdSyncRequest,
    request: Request,
) -> CTFdSyncResponse:
    try:
        config = CTFdConnectionConfig.from_env()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    client = CTFdSourceClient(config)
    try:
        result = await CTFdSynchronizer(_runtime(request).catalog, client).sync(
            competition_id,
            download_files=body.download_files,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="CTFd request failed") from exc
    finally:
        await client.close()
    return CTFdSyncResponse.model_validate(result, from_attributes=True)


async def _catalog_call(function, *args, **kwargs):
    try:
        return await asyncio.to_thread(function, *args, **kwargs)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="resource already exists") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="artifact storage operation failed") from exc
