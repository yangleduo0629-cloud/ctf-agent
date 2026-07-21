from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, WebSocket
from fastapi.websockets import WebSocketDisconnect
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.orm import Session, sessionmaker

from backend.db.enums import ChallengeStatus, SolverRunStatus
from backend.orchestration.event_store import (
    EventStore,
    IdempotencyConflictError,
    event_to_dict,
)
from backend.orchestration.redis_transport import EVENT_CHANNEL, EventRelay, RedisTaskQueue

router = APIRouter()


@dataclass
class OrchestrationRuntime:
    redis: Redis
    session_factory: sessionmaker[Session]
    relay: EventRelay


class ChallengeTransitionRequest(BaseModel):
    target: ChallengeStatus
    expected_version: int | None = Field(default=None, ge=1)
    reason: str | None = Field(default=None, max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SolverRunTransitionRequest(BaseModel):
    target: SolverRunStatus
    expected_version: int | None = Field(default=None, ge=1)
    reason: str | None = Field(default=None, max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CheckpointCreateRequest(BaseModel):
    sequence: int = Field(ge=0)
    state: dict[str, Any]
    storage_key: str | None = Field(default=None, max_length=1024)


class CheckpointRestoreRequest(BaseModel):
    sequence: int | None = Field(default=None, ge=0)


class TaskAccepted(BaseModel):
    task_id: uuid.UUID
    stream_id: str
    duplicate: bool


def _runtime(request: Request) -> OrchestrationRuntime:
    return request.app.state.orchestration


@router.post(
    "/api/challenges/{challenge_id}/transitions",
    response_model=TaskAccepted,
    status_code=202,
)
async def transition_challenge(
    challenge_id: uuid.UUID,
    body: ChallengeTransitionRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> TaskAccepted:
    return await _enqueue(
        _runtime(request).redis,
        "challenge.transition",
        {
            "challenge_id": str(challenge_id),
            "target": body.target.value,
            "expected_version": body.expected_version,
            "reason": body.reason,
            "metadata": body.metadata,
        },
        idempotency_key,
    )


@router.post(
    "/api/solver-runs/{solver_run_id}/transitions",
    response_model=TaskAccepted,
    status_code=202,
)
async def transition_solver_run(
    solver_run_id: uuid.UUID,
    body: SolverRunTransitionRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> TaskAccepted:
    return await _enqueue(
        _runtime(request).redis,
        "solver_run.transition",
        {
            "solver_run_id": str(solver_run_id),
            "target": body.target.value,
            "expected_version": body.expected_version,
            "reason": body.reason,
            "metadata": body.metadata,
        },
        idempotency_key,
    )


@router.post(
    "/api/solver-runs/{solver_run_id}/checkpoints",
    response_model=TaskAccepted,
    status_code=202,
)
async def create_checkpoint(
    solver_run_id: uuid.UUID,
    body: CheckpointCreateRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> TaskAccepted:
    return await _enqueue(
        _runtime(request).redis,
        "checkpoint.create",
        {
            "solver_run_id": str(solver_run_id),
            "sequence": body.sequence,
            "state": body.state,
            "storage_key": body.storage_key,
        },
        idempotency_key,
    )


@router.post(
    "/api/solver-runs/{solver_run_id}/restore",
    response_model=TaskAccepted,
    status_code=202,
)
async def restore_checkpoint(
    solver_run_id: uuid.UUID,
    body: CheckpointRestoreRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> TaskAccepted:
    return await _enqueue(
        _runtime(request).redis,
        "checkpoint.restore",
        {
            "solver_run_id": str(solver_run_id),
            "sequence": body.sequence,
        },
        idempotency_key,
    )


@router.get("/api/events")
async def list_events(
    request: Request,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> list[dict[str, object]]:
    runtime = _runtime(request)
    return await asyncio.to_thread(
        _list_events,
        runtime.session_factory,
        after_sequence,
        limit,
    )


@router.websocket("/ws/events")
async def event_websocket(
    websocket: WebSocket,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
) -> None:
    runtime: OrchestrationRuntime = websocket.app.state.orchestration
    pubsub = runtime.redis.pubsub()
    await websocket.accept()
    await pubsub.subscribe(EVENT_CHANNEL)
    last_sequence = after_sequence
    try:
        while True:
            replay = await asyncio.to_thread(
                _list_events,
                runtime.session_factory,
                last_sequence,
                500,
            )
            for event in replay:
                await websocket.send_json(event)
                last_sequence = _event_sequence(event)
            if len(replay) < 500:
                break

        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=5.0,
            )
            if message is None:
                await websocket.send_json(
                    {"type": "heartbeat", "after_sequence": last_sequence}
                )
                continue
            event = json.loads(message["data"])
            sequence = int(event["sequence"])
            if sequence <= last_sequence:
                continue
            await websocket.send_json(event)
            last_sequence = sequence
    except WebSocketDisconnect:
        pass
    finally:
        await pubsub.unsubscribe(EVENT_CHANNEL)
        await pubsub.aclose()


async def _enqueue(
    redis: Redis,
    task_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
) -> TaskAccepted:
    queue = RedisTaskQueue(redis)
    try:
        task_id, stream_id, duplicate = await queue.enqueue(
            task_type,
            payload,
            idempotency_key=idempotency_key,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return TaskAccepted(task_id=task_id, stream_id=stream_id, duplicate=duplicate)


def _list_events(
    session_factory: sessionmaker[Session],
    after_sequence: int,
    limit: int,
) -> list[dict[str, object]]:
    with session_factory() as session:
        return [
            event_to_dict(event)
            for event in EventStore(session).list_after(after_sequence, limit=limit)
        ]


def _event_sequence(event: dict[str, object]) -> int:
    sequence = event["sequence"]
    if not isinstance(sequence, int):
        raise TypeError("event sequence must be an integer")
    return sequence
