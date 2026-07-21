from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import uuid

from redis.asyncio import Redis, from_url
from sqlalchemy.orm import Session, sessionmaker

from backend.db.enums import ChallengeStatus, SolverRunStatus
from backend.db.session import create_database_engine, create_session_factory, session_scope
from backend.orchestration.redis_transport import (
    EventRelay,
    LockUnavailableError,
    QueuedTask,
    RedisLeaseLock,
    RedisTaskQueue,
)
from backend.orchestration.services import CheckpointService, StateTransitionService

WORKER_HEARTBEAT = "ctf:worker:heartbeat"
logger = logging.getLogger(__name__)


class StateTaskWorker:
    def __init__(
        self,
        redis: Redis,
        session_factory: sessionmaker[Session],
        *,
        consumer: str | None = None,
    ) -> None:
        self.redis = redis
        self.session_factory = session_factory
        self.consumer = consumer or f"{socket.gethostname()}-{os.getpid()}"
        self.queue = RedisTaskQueue(redis)

    async def process_next(
        self,
        *,
        block_ms: int = 5000,
        claim_idle_ms: int = 30_000,
    ) -> bool:
        tasks = await self.queue.read(
            self.consumer,
            count=1,
            block_ms=block_ms,
            claim_idle_ms=claim_idle_ms,
        )
        if not tasks:
            return False
        task = tasks[0]
        lock_key = self._lock_key(task)
        async with RedisLeaseLock(self.redis, lock_key, lease_ms=30_000) as lease:
            await asyncio.to_thread(self._dispatch, task)
            if lease.lost:
                raise LockUnavailableError(f"task lock lease lost: {lock_key}")
            await self.queue.acknowledge(task)
        return True

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.redis.set(WORKER_HEARTBEAT, self.consumer, ex=15)
            try:
                await self.process_next()
            except Exception:
                logger.exception("task processing failed")
                await asyncio.sleep(1)

    def _dispatch(self, task: QueuedTask) -> None:
        with session_scope(self.session_factory) as session:
            payload = task.payload
            if task.task_type == "challenge.transition":
                StateTransitionService(session).transition_challenge(
                    uuid.UUID(payload["challenge_id"]),
                    ChallengeStatus(payload["target"]),
                    idempotency_key=task.idempotency_key,
                    expected_version=payload.get("expected_version"),
                    reason=payload.get("reason"),
                    metadata=payload.get("metadata"),
                )
            elif task.task_type == "solver_run.transition":
                StateTransitionService(session).transition_solver_run(
                    uuid.UUID(payload["solver_run_id"]),
                    SolverRunStatus(payload["target"]),
                    idempotency_key=task.idempotency_key,
                    expected_version=payload.get("expected_version"),
                    reason=payload.get("reason"),
                    metadata=payload.get("metadata"),
                )
            elif task.task_type == "checkpoint.create":
                CheckpointService(session).create(
                    uuid.UUID(payload["solver_run_id"]),
                    int(payload["sequence"]),
                    payload["state"],
                    idempotency_key=task.idempotency_key,
                    storage_key=payload.get("storage_key"),
                )
            elif task.task_type == "checkpoint.restore":
                CheckpointService(session).restore(
                    uuid.UUID(payload["solver_run_id"]),
                    idempotency_key=task.idempotency_key,
                    sequence=(int(payload["sequence"]) if payload.get("sequence") else None),
                )
            else:
                raise ValueError(f"unknown task type: {task.task_type}")

    @staticmethod
    def _lock_key(task: QueuedTask) -> str:
        if challenge_id := task.payload.get("challenge_id"):
            return f"aggregate:challenge:{challenge_id}"
        if solver_run_id := task.payload.get("solver_run_id"):
            return f"aggregate:solver-run:{solver_run_id}"
        return f"task:{task.task_id}"


async def run_worker(*, once: bool = False) -> None:
    redis = from_url(os.environ["REDIS_URL"], decode_responses=True)
    engine = create_database_engine(os.environ["DATABASE_URL"])
    factory = create_session_factory(engine)
    worker = StateTaskWorker(redis, factory)
    relay = EventRelay(redis, factory)
    try:
        if once:
            await worker.process_next(block_ms=5000, claim_idle_ms=0)
            await relay.run_once()
            return
        stop = asyncio.Event()
        await asyncio.gather(worker.run(stop), relay.run(stop))
    finally:
        await redis.aclose()
        engine.dispose()


async def healthcheck() -> int:
    redis = from_url(os.environ["REDIS_URL"], decode_responses=True)
    try:
        return 0 if await redis.exists(WORKER_HEARTBEAT) else 1
    finally:
        await redis.aclose()


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    if args.healthcheck:
        return asyncio.run(healthcheck())
    asyncio.run(run_worker(once=args.once))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
