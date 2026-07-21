# Domain data model

The persistence layer lives in `backend.db` and uses SQLAlchemy 2 typed declarative mappings.
Alembic revision `20260721_0001` creates the entities in this order:

1. `Competition`
2. `Challenge`
3. `Artifact`
4. `SolverRun`
5. `Checkpoint`
6. `ModelCall`
7. `ToolCall`
8. `Evidence`
9. `FlagCandidate`
10. `Submission`
11. `EvalRun`

Every entity has a UUID primary key, UTC `created_at` and `updated_at` timestamps, an optimistic
locking `version`, and nullable `deleted_at`. Repository reads hide soft-deleted records by default.

```mermaid
erDiagram
    Competition ||--o{ Challenge : contains
    Competition ||--o{ EvalRun : evaluates
    Challenge ||--o{ Artifact : attaches
    Challenge ||--o{ SolverRun : executes
    SolverRun ||--o{ Checkpoint : checkpoints
    SolverRun ||--o{ ModelCall : invokes
    SolverRun ||--o{ ToolCall : records
    ModelCall o|--o{ ToolCall : requests
    SolverRun ||--o{ Evidence : produces
    Artifact o|--o{ Evidence : supports
    Evidence o|--o{ FlagCandidate : supports
    FlagCandidate o|--o{ Submission : submits
```

## Deletion and artifact retention

`Repositories.soft_delete_competition()` marks the competition, challenges, solver runs, events,
checkpoints, evidence, candidates, submissions, and evaluation runs as deleted in one transaction.
Foreign keys use `RESTRICT` or `SET NULL`; normal application flows do not physically cascade rows.

Artifacts follow one of three policies:

| Policy | Parent deletion behavior | Storage purge eligibility |
|---|---|---|
| `keep` | Artifact remains visible | Never selected automatically |
| `delete_with_parent` | Soft-deleted with parent | Immediately eligible |
| `expires_at` | Soft-deleted with parent | Eligible after `retain_until` |

`ArtifactRepository.due_for_purge()` returns storage keys eligible for object deletion.
The storage worker deletes the bytes first, then calls `mark_purged()` to preserve the audit row,
hash, original filename, and purge timestamp.

## Commands

```bash
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE uv run alembic upgrade head
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE uv run python -m backend.db.verify
```

The FastAPI container runs `alembic upgrade head` before Uvicorn starts and reports the active schema
revision from `/readyz`.
