# Challenge ingestion

Local exercises and CTFd challenges use the same domain path:

```text
Competition -> Challenge -> Artifact -> SolverRun
```

Alembic revision `20260721_0003` adds competition flag rules, parsed service endpoint fields,
and artifact source URLs. Challenge metadata synchronization and artifact creation also append
immutable domain events. The first available local artifact or successful CTFd metadata sync moves
a challenge from `NEW` to `INGESTED` in the same database transaction.

## Storage

The API streams uploaded and downloaded content into
`/srv/ctf-platform/data/artifacts/<challenge-uuid>/`. It calculates SHA-256 while writing, detects
common file signatures before considering the declared MIME type, and calls `fsync` before the
database record is committed. Failed transactions remove the new file. A repeated SHA-256 for the
same challenge reuses the existing artifact row and physical file.

The default per-file limit is 512 MiB. Set `ARTIFACT_MAX_SIZE_BYTES` in `infra/wsl/.env` to change
it. Active artifacts remain on the WSL ext4 filesystem; `/mnt/f` remains backup-only.

## API

All local mutation requests require a stable `Idempotency-Key` header:

| Operation | Endpoint |
|---|---|
| Create competition | `POST /api/competitions` |
| Create local challenge | `POST /api/competitions/{competition_id}/challenges` |
| Upload attachment | `POST /api/challenges/{challenge_id}/artifacts` |
| Create solver run | `POST /api/challenges/{challenge_id}/solver-runs` |
| Synchronize CTFd | `POST /api/competitions/{competition_id}/ctfd/sync` |

The upload endpoint accepts multipart field `file` and returns the stored name, storage key, byte
size, SHA-256, detected MIME type, and duplicate status. Challenge responses include both the
original `connection_info` and parsed `service_protocol`, `service_host`, and `service_port`.

Example local workflow:

```bash
curl -sS -X POST http://127.0.0.1:8080/api/competitions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: competition:local-exercises' \
  -d '{"name":"Local Exercises","slug":"local-exercises","platform":"local","flag_format":"flag{...}","flag_regex":"^flag\\{[^}\\r\\n]+\\}$"}'

curl -sS -X POST http://127.0.0.1:8080/api/competitions/COMPETITION_ID/challenges \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: challenge:simple-socket' \
  -d '{"name":"SimpleSocket","slug":"simple-socket","category":"misc"}'

curl -sS -X POST http://127.0.0.1:8080/api/challenges/CHALLENGE_ID/artifacts \
  -H 'Idempotency-Key: artifact:simple-socket' \
  -F 'file=@SimpleSocket.zip'

curl -sS -X POST http://127.0.0.1:8080/api/challenges/CHALLENGE_ID/solver-runs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: solver-run:simple-socket-1' \
  -d '{"run_key":"simple-socket-1","solver_type":"agent","model_spec":"MODEL"}'
```

## CTFd connection

Create a competition with `platform` set to `ctfd`, then configure the API container through
`infra/wsl/.env`:

```env
CTFD_URL=https://ctfd.example
CTFD_TOKEN=
CTFD_USERNAME=
CTFD_PASSWORD=
CTFD_VERIFY_TLS=true
CTFD_TIMEOUT_SECONDS=30
```

Use either a token or username/password. Credentials are read only from process environment and are
excluded from competition details, events, logs, images, Git, and API responses. Trigger a sync with:

```bash
curl -sS -X POST http://127.0.0.1:8080/api/competitions/COMPETITION_ID/ctfd/sync \
  -H 'Content-Type: application/json' \
  -d '{"download_files":true}'
```

Synchronization follows CTFd pagination, loads each challenge detail, upserts by competition and
external challenge ID, records tags/hints/solve counts, parses connection information, and downloads
each attachment. Repeating a sync updates changed metadata while reusing identical attachments.
