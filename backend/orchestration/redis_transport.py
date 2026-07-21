from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import uuid
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy.orm import Session, sessionmaker

from backend.db.session import session_scope
from backend.orchestration.event_store import (
    EventStore,
    IdempotencyConflictError,
    event_to_dict,
)

logger = logging.getLogger(__name__)

TASK_STREAM = "ctf:tasks"
TASK_GROUP = "ctf-workers"
EVENT_STREAM = "ctf:events"
EVENT_CHANNEL = "ctf:events:live"
EVENT_TRANSPORT = "redis-stream"

ENQUEUE_SCRIPT = """
local existing = redis.call('GET', KEYS[2])
if existing then
    return existing
end
local stream_id = redis.call(
    'XADD', KEYS[1], '*',
    'task_id', ARGV[1],
    'task_type', ARGV[2],
    'payload', ARGV[3],
    'idempotency_key', ARGV[4]
)
local result = ARGV[6] .. '|' .. ARGV[1] .. '|' .. stream_id
redis.call('SET', KEYS[2], result, 'EX', ARGV[5])
return result
"""

RENEW_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class LockUnavailableError(TimeoutError):
    pass


@dataclass(frozen=True)
class QueuedTask:
    stream_id: str
    task_id: uuid.UUID
    task_type: str
    payload: dict[str, Any]
    idempotency_key: str


class RedisTaskQueue:
    def __init__(
        self,
        redis: Redis,
        *,
        stream: str = TASK_STREAM,
        group: str = TASK_GROUP,
        idempotency_ttl_seconds: int = 7 * 24 * 60 * 60,
    ) -> None:
        self.redis = redis
        self.stream = stream
        self.group = group
        self.idempotency_ttl_seconds = idempotency_ttl_seconds

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(self.stream, self.group, id="0-0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def enqueue(
        self,
        task_type: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
        task_id: uuid.UUID | None = None,
    ) -> tuple[uuid.UUID, str, bool]:
        identifier = task_id or uuid.uuid4()
        payload_json = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))
        key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
        operation_hash = hashlib.sha256(f"{task_type}\0{payload_json}".encode()).hexdigest()
        result = str(
            await _redis_eval(
                self.redis,
                ENQUEUE_SCRIPT,
                2,
                self.stream,
                f"{self.stream}:idempotency:{key_hash}",
                str(identifier),
                task_type,
                payload_json,
                idempotency_key,
                str(self.idempotency_ttl_seconds),
                operation_hash,
            )
        )
        existing_hash, existing_task_id, stream_id = result.split("|", maxsplit=2)
        if existing_hash != operation_hash:
            raise IdempotencyConflictError(
                f"idempotency key {idempotency_key!r} belongs to another task"
            )
        existing_identifier = uuid.UUID(existing_task_id)
        return existing_identifier, stream_id, existing_identifier != identifier

    async def read(
        self,
        consumer: str,
        *,
        count: int = 1,
        block_ms: int = 5000,
        claim_idle_ms: int = 30_000,
    ) -> list[QueuedTask]:
        await self.ensure_group()
        claimed = await self.redis.xautoclaim(
            self.stream,
            self.group,
            consumer,
            min_idle_time=claim_idle_ms,
            start_id="0-0",
            count=count,
        )
        claimed_messages = claimed[1] if len(claimed) > 1 else []
        if claimed_messages:
            return [self._decode(stream_id, fields) for stream_id, fields in claimed_messages]

        response = await self.redis.xreadgroup(
            self.group,
            consumer,
            {self.stream: ">"},
            count=count,
            block=block_ms,
        )
        if not response:
            return []
        return [
            self._decode(stream_id, fields)
            for _stream, messages in response
            for stream_id, fields in messages
        ]

    async def acknowledge(self, task: QueuedTask) -> None:
        await self.redis.xack(self.stream, self.group, task.stream_id)

    async def pending_count(self) -> int:
        summary = await self.redis.xpending(self.stream, self.group)
        return int(summary["pending"])

    @staticmethod
    def _decode(stream_id: str, fields: Mapping[str, str]) -> QueuedTask:
        return QueuedTask(
            stream_id=str(stream_id),
            task_id=uuid.UUID(fields["task_id"]),
            task_type=fields["task_type"],
            payload=json.loads(fields["payload"]),
            idempotency_key=fields["idempotency_key"],
        )


class RedisLeaseLock:
    def __init__(
        self,
        redis: Redis,
        key: str,
        *,
        lease_ms: int = 30_000,
        acquire_timeout: float = 10.0,
    ) -> None:
        self.redis = redis
        self.key = f"ctf:lock:{key}"
        self.lease_ms = lease_ms
        self.acquire_timeout = acquire_timeout
        self.token = secrets.token_hex(24)
        self.lost = False
        self._renew_task: asyncio.Task[None] | None = None

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.acquire_timeout
        while True:
            acquired = await self.redis.set(self.key, self.token, nx=True, px=self.lease_ms)
            if acquired:
                self._renew_task = asyncio.create_task(self._renew_loop())
                return
            if loop.time() >= deadline:
                raise LockUnavailableError(f"lock acquisition timed out: {self.key}")
            await asyncio.sleep(0.1)

    async def release(self) -> None:
        if self._renew_task is not None:
            self._renew_task.cancel()
            try:
                await self._renew_task
            except asyncio.CancelledError:
                pass
            self._renew_task = None
        await _redis_eval(self.redis, RELEASE_LOCK_SCRIPT, 1, self.key, self.token)

    async def _renew_loop(self) -> None:
        interval = max(self.lease_ms / 3000, 0.1)
        while True:
            await asyncio.sleep(interval)
            renewed = await _redis_eval(
                self.redis,
                RENEW_LOCK_SCRIPT,
                1,
                self.key,
                self.token,
                str(self.lease_ms),
            )
            if not renewed:
                self.lost = True
                return

    async def __aenter__(self) -> RedisLeaseLock:
        await self.acquire()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.release()


class RedisEventPublisher:
    def __init__(
        self,
        redis: Redis,
        *,
        stream: str = EVENT_STREAM,
        channel: str = EVENT_CHANNEL,
    ) -> None:
        self.redis = redis
        self.stream = stream
        self.channel = channel

    async def publish(self, event: Mapping[str, object]) -> None:
        sequence_value = event["sequence"]
        if not isinstance(sequence_value, int):
            raise TypeError("event sequence must be an integer")
        sequence = sequence_value
        stream_id = f"{sequence}-0"
        payload = json.dumps(dict(event), sort_keys=True, separators=(",", ":"))
        try:
            await self.redis.xadd(self.stream, {"event": payload}, id=stream_id)
        except ResponseError as exc:
            if "equal or smaller" not in str(exc):
                raise
        await self.redis.publish(self.channel, payload)


class EventRelay:
    def __init__(
        self,
        redis: Redis,
        session_factory: sessionmaker[Session],
        *,
        poll_interval: float = 0.5,
        stream: str = EVENT_STREAM,
        channel: str = EVENT_CHANNEL,
        transport: str = EVENT_TRANSPORT,
    ) -> None:
        self.redis = redis
        self.session_factory = session_factory
        self.poll_interval = poll_interval
        self.transport = transport
        self.publisher = RedisEventPublisher(redis, stream=stream, channel=channel)

    async def run_once(self, *, limit: int = 100) -> int:
        try:
            async with RedisLeaseLock(
                self.redis,
                "event-relay",
                lease_ms=15_000,
                acquire_timeout=0.2,
            ):
                pending = await asyncio.to_thread(self._pending, limit)
                published = 0
                for event in pending:
                    try:
                        await self.publisher.publish(event)
                    except Exception as exc:
                        await asyncio.to_thread(
                            self._record_delivery,
                            uuid.UUID(str(event["id"])),
                            str(exc),
                        )
                        raise
                    await asyncio.to_thread(
                        self._record_delivery,
                        uuid.UUID(str(event["id"])),
                        None,
                    )
                    published += 1
                return published
        except LockUnavailableError:
            return 0

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                published = await self.run_once()
            except Exception:
                logger.exception("event relay iteration failed")
                published = 0
            delay = 0 if published else self.poll_interval
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    def _pending(self, limit: int) -> list[dict[str, object]]:
        with self.session_factory() as session:
            return [
                event_to_dict(event)
                for event in EventStore(session).pending_delivery(self.transport, limit=limit)
            ]

    def _record_delivery(self, event_id: uuid.UUID, error: str | None) -> None:
        with session_scope(self.session_factory) as session:
            EventStore(session).record_delivery(event_id, self.transport, error=error)


async def _redis_eval(redis: Redis, script: str, numkeys: int, *values: str) -> Any:
    result = redis.eval(script, numkeys, *values)
    return await cast(Awaitable[Any], result)
