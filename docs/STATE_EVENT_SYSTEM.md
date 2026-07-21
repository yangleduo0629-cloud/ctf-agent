# State and event system

## State machines

Challenge transitions are validated by `backend.orchestration.state_machines`.

```text
NEW -> INGESTED -> CLASSIFIED -> READY -> SOLVING
                                      -> CANDIDATE -> VERIFYING -> SOLVED -> SUBMITTED
                                           RETRY <-/    |  |
                                                   REVIEW
```

`RETRY` can resume at `READY` or `SOLVING`. `REVIEW` can return to `READY`, continue at
`VERIFYING`, or approve `SOLVED`. `SUBMITTED` is terminal.

Solver runs use:

```text
QUEUED -> RUNNING -> SUCCEEDED
   ^         |----> FAILED -> QUEUED
   |         |----> CANCELLED
   +---------+
```

The `RUNNING -> QUEUED` edge is reserved for lease loss and restart recovery.

## Transaction boundary

Every state update follows one database transaction:

1. Lock the aggregate row with `SELECT ... FOR UPDATE`.
2. Resolve and validate the idempotency key.
3. Check the expected aggregate version when supplied.
4. Validate the transition graph and update the aggregate.
5. Append a `domain_events` row with the resulting aggregate version.
6. Commit the aggregate and event together.

`domain_events` has no update, delete, version, or soft-delete fields. PostgreSQL and SQLite
triggers reject updates and deletes. Delivery state is recorded separately in
`event_deliveries`.

## Redis transport

| Purpose | Redis key |
|---|---|
| Task stream | `ctf:tasks` |
| Worker consumer group | `ctf-workers` |
| Event stream | `ctf:events` |
| Live WebSocket channel | `ctf:events:live` |
| Aggregate locks | `ctf:lock:aggregate:*` |
| Worker heartbeat | `ctf:worker:heartbeat` |

Task enqueue uses a Lua script to bind one idempotency key to one payload and stream entry.
Workers ACK only after the database transaction commits. A replacement worker reclaims stale
pending entries with `XAUTOCLAIM`; replaying the task returns the existing domain event.

Locks use random owner tokens. Renewal and release are compare-and-act Lua scripts, so an expired
worker cannot renew or delete another worker's lease.

## Event publication and WebSocket replay

API and Worker processes both run the database outbox relay. A global Redis lease selects one
relay at a time. Events are written to the Redis stream with `<database-sequence>-0` IDs and then
published to the live channel. Delivery completion is stored only after Redis accepts the event.

WebSocket clients connect to `/ws/events?after_sequence=N`. The API subscribes first, replays
database events after `N`, and then forwards live messages while suppressing duplicate sequence
numbers. `GET /api/events?after_sequence=N` provides the same database replay over HTTP.

## Task endpoints

All mutation endpoints require an `Idempotency-Key` header and return HTTP 202 with the task UUID
and Redis stream ID.

- `POST /api/challenges/{id}/transitions`
- `POST /api/solver-runs/{id}/transitions`
- `POST /api/solver-runs/{id}/checkpoints`
- `POST /api/solver-runs/{id}/restore`

## Checkpoint recovery

Checkpoint state is serialized with sorted keys and compact separators, then protected by a
SHA-256 checksum. Restore verifies the checksum and records the chosen checkpoint ID and sequence
in `SolverRun.metrics.resume_checkpoint`. Creation and restore each append an immutable event.
