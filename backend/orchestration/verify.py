from __future__ import annotations

import asyncio
import json
import os
import uuid

from redis.asyncio import from_url
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from backend.db.enums import ChallengeStatus
from backend.db.models import Challenge, Checkpoint, Competition, DomainEvent, SolverRun
from backend.db.session import create_database_engine, create_session_factory, session_scope
from backend.orchestration.event_store import IdempotencyConflictError
from backend.orchestration.redis_transport import (
    EventRelay,
    LockUnavailableError,
    RedisLeaseLock,
    RedisTaskQueue,
)
from backend.orchestration.worker import StateTaskWorker


async def verify_recovery(database_url: str, redis_url: str) -> dict[str, object]:
    marker = uuid.uuid4().hex
    prefix = f"ctf:verify:{marker}"
    redis = from_url(redis_url, decode_responses=True)
    engine = create_database_engine(database_url)
    factory = create_session_factory(engine)
    queue = RedisTaskQueue(redis, stream=f"{prefix}:tasks", group="verify-workers")
    event_stream = f"{prefix}:events"
    try:
        challenge_id, solver_run_id = await asyncio.to_thread(_create_fixture, factory, marker)
        payload = {"challenge_id": str(challenge_id), "target": "ingested"}
        task_id, stream_id, duplicate = await queue.enqueue(
            "challenge.transition",
            payload,
            idempotency_key=f"verify:{marker}:ingest",
        )
        repeated_task_id, repeated_stream_id, repeated = await queue.enqueue(
            "challenge.transition",
            payload,
            idempotency_key=f"verify:{marker}:ingest",
        )
        if duplicate or not repeated:
            raise RuntimeError("queue idempotency result was inconsistent")
        if (task_id, stream_id) != (repeated_task_id, repeated_stream_id):
            raise RuntimeError("queue idempotency returned a different task")
        try:
            await queue.enqueue(
                "challenge.transition",
                {"challenge_id": str(challenge_id), "target": "classified"},
                idempotency_key=f"verify:{marker}:ingest",
            )
        except IdempotencyConflictError:
            pass
        else:
            raise RuntimeError("queue accepted conflicting idempotency payload")

        claimed = await queue.read(
            "worker-before-crash",
            block_ms=100,
            claim_idle_ms=0,
        )
        if len(claimed) != 1:
            raise RuntimeError("initial worker did not claim the task")
        crashed_worker = StateTaskWorker(redis, factory, consumer="worker-before-crash")
        crashed_worker.queue = queue
        await asyncio.to_thread(crashed_worker._dispatch, claimed[0])

        recovery_worker = StateTaskWorker(redis, factory, consumer="worker-after-restart")
        recovery_worker.queue = queue
        recovered = await recovery_worker.process_next(block_ms=100, claim_idle_ms=0)
        if not recovered or await queue.pending_count() != 0:
            raise RuntimeError("restarted worker did not reclaim and acknowledge the task")

        await _verify_lock_contention(redis, prefix)

        await queue.enqueue(
            "checkpoint.create",
            {
                "solver_run_id": str(solver_run_id),
                "sequence": 1,
                "state": {"phase": "solving", "step": 7},
                "storage_key": f"verification/{marker}/checkpoint.json",
            },
            idempotency_key=f"verify:{marker}:checkpoint-create",
        )
        await recovery_worker.process_next(block_ms=100, claim_idle_ms=0)
        await queue.enqueue(
            "checkpoint.restore",
            {"solver_run_id": str(solver_run_id), "sequence": 1},
            idempotency_key=f"verify:{marker}:checkpoint-restore",
        )
        await recovery_worker.process_next(block_ms=100, claim_idle_ms=0)

        relay = EventRelay(
            redis,
            factory,
            stream=event_stream,
            channel=f"{prefix}:live",
            transport=f"verify-{marker[:16]}",
        )
        published = await relay.run_once()
        database_state = await asyncio.to_thread(
            _verify_database_state,
            factory,
            challenge_id,
            solver_run_id,
        )
        stream_length = await redis.xlen(event_stream)
        await asyncio.to_thread(_verify_database_immutability, factory)
        if stream_length != published or published != database_state["event_count"]:
            raise RuntimeError("database events and Redis event stream diverged")

        return {
            "status": "passed",
            "challenge_id": str(challenge_id),
            "solver_run_id": str(solver_run_id),
            "reclaimed_stream_id": stream_id,
            "pending_tasks": await queue.pending_count(),
            "event_count": database_state["event_count"],
            "checkpoint_sequence": database_state["checkpoint_sequence"],
            "redis_event_count": stream_length,
            "database_event_guard": "passed",
            "distributed_lock": "passed",
        }
    finally:
        keys = [key async for key in redis.scan_iter(match=f"{prefix}*")]
        if keys:
            await redis.delete(*keys)
        await redis.aclose()
        engine.dispose()


def _create_fixture(factory, marker: str) -> tuple[uuid.UUID, uuid.UUID]:
    with session_scope(factory) as session:
        competition = Competition(name=f"Recovery {marker}", slug=f"recovery-{marker}")
        session.add(competition)
        session.flush()
        challenge = Challenge(
            competition_id=competition.id,
            slug="restart-recovery",
            name="Restart Recovery",
            category="verification",
        )
        session.add(challenge)
        session.flush()
        solver_run = SolverRun(
            challenge_id=challenge.id,
            run_key="recovery-run",
            solver_type="verification",
            model_spec="fixture/model",
        )
        session.add(solver_run)
        session.flush()
        return challenge.id, solver_run.id


def _verify_database_state(
    factory,
    challenge_id: uuid.UUID,
    solver_run_id: uuid.UUID,
) -> dict[str, int]:
    with factory() as session:
        challenge = session.get(Challenge, challenge_id)
        solver_run = session.get(SolverRun, solver_run_id)
        checkpoint = session.scalar(
            select(Checkpoint)
            .where(Checkpoint.solver_run_id == solver_run_id)
            .order_by(Checkpoint.sequence.desc())
            .limit(1)
        )
        event_count = session.scalar(select(func.count()).select_from(DomainEvent)) or 0
        if challenge is None or challenge.status != ChallengeStatus.INGESTED:
            raise RuntimeError("challenge transition was not preserved")
        if solver_run is None or solver_run.metrics.get("resume_checkpoint") is None:
            raise RuntimeError("checkpoint restore marker was not preserved")
        if checkpoint is None or checkpoint.checksum is None:
            raise RuntimeError("checkpoint was not persisted with a checksum")
        return {"event_count": event_count, "checkpoint_sequence": checkpoint.sequence}


def _verify_database_immutability(factory) -> None:
    with factory() as session:
        event_id = session.scalar(select(DomainEvent.id).limit(1))
        if event_id is None:
            raise RuntimeError("no event available for immutability probe")
        try:
            session.execute(
                text("UPDATE domain_events SET event_type = 'tampered' WHERE id = :event_id"),
                {"event_id": event_id},
            )
            session.commit()
        except DBAPIError:
            session.rollback()
            return
        raise RuntimeError("database accepted a domain event update")


async def _verify_lock_contention(redis, prefix: str) -> None:
    first = RedisLeaseLock(redis, f"{prefix}:lock", acquire_timeout=0.2)
    second = RedisLeaseLock(redis, f"{prefix}:lock", acquire_timeout=0.2)
    await first.acquire()
    try:
        try:
            await second.acquire()
        except LockUnavailableError:
            pass
        else:
            raise RuntimeError("two workers acquired the same distributed lock")
    finally:
        await first.release()
    await second.acquire()
    await second.release()


def main() -> None:
    result = asyncio.run(
        verify_recovery(
            os.environ["DATABASE_URL"],
            os.environ["REDIS_URL"],
        )
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
