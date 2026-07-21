# Docker sandbox runner

The first sandbox runner executes one command or script in one disposable container. Public API
requests are associated with an existing `SolverRun`, which links every execution to its challenge.

```text
API -> internal sandbox-runner -> Docker Engine -> disposable container
 |                                      |
 +-> ToolCall + LOG Evidence            +-> isolated workspace
 +-> Artifact + FILE Evidence <---------+   generated files
```

## Public API

Use `POST /api/solver-runs/{solver_run_id}/sandbox/executions` with an `Idempotency-Key` header.

Command:

```json
{
  "mode": "command",
  "command": "sha256sum /attachments/input.zip && printf result > result.txt",
  "limits": {
    "timeout_seconds": 30,
    "cpu_cores": 1.0,
    "memory_mb": 512,
    "pids_limit": 64
  }
}
```

Python script:

```json
{
  "mode": "python",
  "script": "from pathlib import Path\nPath('result.txt').write_text('ok')\nprint('done')",
  "args": []
}
```

Shell script uses `"mode": "shell"` and the same `script` and `args` fields. The response contains
the ToolCall ID, sequence, status, stdout, stderr, exit code, timeout state, duration, truncation
state, and archived output artifacts. Replaying the same key and payload returns the stored result.

## Filesystem contract

| Container path | Access | Source |
|---|---|---|
| `/attachments` | read-only | Original active challenge artifacts |
| `/workspace` | read-write | Empty per-execution directory |
| `/tmp` | read-write tmpfs | 64 MiB, no executable files |
| `/run` | read-write tmpfs | 16 MiB, no executable files |

Host staging uses
`/srv/ctf-platform/data/sandboxes/<challenge-id>/<tool-call-id>/`. The runner removes its injected
script before scanning the workspace. Regular files are copied into the permanent Artifact store;
symlinks and special files are skipped. Staging is removed after database finalization or failure
and is intentionally absent from backups.

The default archive bounds are 100 files and 256 MiB total. Stdout and stderr are each retained up
to 1 MiB and report whether truncation occurred.

## Container isolation

The execution container uses the pinned `python:3.12.11-slim-bookworm` base and runs as
`65532:65532`. Every execution applies:

- no network namespace connectivity;
- read-only root filesystem;
- all Linux capabilities dropped;
- `no-new-privileges`;
- CPU, memory, equal memory/swap, PID, file descriptor, and core dump limits;
- bounded local Docker logs;
- forced container deletion after success, non-zero exit, timeout, or transport failure.

The public API container has no Docker socket. A dedicated Compose-internal service owns the socket
and listens on port `8090` only inside the internal network. API-to-runner requests require the
random `SANDBOX_RUNNER_TOKEN` stored in `infra/wsl/.env`.
