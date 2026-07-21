# WSL foundation environment

This directory defines the reproducible Ubuntu 24.04 foundation for the CTF platform.

## Storage contract

| Path | Purpose |
|---|---|
| `F:\WSL\Ubuntu-24.04\ext4.vhdx` | WSL2 distribution VHDX |
| `/srv/ctf-platform/app` | Git workspace on WSL ext4 |
| `/srv/ctf-platform/data/postgres` | PostgreSQL 16 data |
| `/srv/ctf-platform/data/redis` | Redis 7 persistence |
| `/srv/ctf-platform/data/artifacts` | Challenge artifacts |
| `/srv/ctf-platform/data/checkpoints` | Task checkpoints |
| `/srv/ctf-platform/models` | Reserved local model directory |
| `/var/lib/docker` | Native Docker Engine data |
| `F:\CTF-Agent-Backups` | Exports and offline backups only |

The active paths above must resolve to an ext4 source. `/mnt/f` is used by the backup scripts only.
Local model weights and runtime installation are outside this bootstrap. LiteLLM reserves the upstream
endpoint `http://host.docker.internal:8001/v1` for that later installation.

## Host configuration

Run from an elevated PowerShell session after the distribution is imported at the required path:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\infra\wsl\host\Configure-Wsl.ps1
```

The script validates the VHDX path and writes a 10 GB memory, 12 CPU, 4 GB swap WSL2 profile with
localhost forwarding. It then shuts WSL down so the resource profile applies on the next start.

## Install and deploy

Run inside `Ubuntu-24.04`:

```bash
cd /srv/ctf-platform/app
sudo infra/wsl/scripts/bootstrap.sh
sudo infra/wsl/scripts/deploy.sh
sudo infra/wsl/scripts/health-check.sh
```

When a Windows TUN client keeps its HTTP proxy on loopback, start the adapter-scoped relay first:

```powershell
$proxy = .\infra\wsl\host\Start-WslProxyRelay.ps1
wsl.exe -d Ubuntu-24.04 -u root -- env HTTP_PROXY=$proxy HTTPS_PROXY=$proxy `
  /srv/ctf-platform/app/infra/wsl/scripts/bootstrap.sh
```

The relay binds only to the WSL virtual adapter. `bootstrap.sh` passes the same endpoint to the native
Docker daemon; Compose passes standard proxy build arguments without writing the proxy into images.

`deploy.sh` creates `infra/wsl/.env` with random local secrets and mode `0600`. The file is ignored by
Git. It builds and starts PostgreSQL, Redis, FastAPI, React/Vite, and LiteLLM in one Compose project.

Only the following host bindings exist:

| Service | Binding |
|---|---|
| React/Vite | `127.0.0.1:3000` |
| FastAPI | `127.0.0.1:8080` |

LiteLLM listens on port `4000` inside Compose. PostgreSQL `5432` and Redis `6379` are restricted to the
internal Compose network. The future model service retains port `8001` without being installed here.

## Persistence and restore verification

The host-side test exercises a complete WSL shutdown and checks PostgreSQL, Redis, artifacts, and
checkpoints after services restart:

```powershell
.\infra\wsl\host\Verify-Persistence.ps1
```

Create an offline backup and verify it in isolated PostgreSQL and Redis fixtures:

```bash
token="restore-$(date +%s)"
sudo infra/wsl/scripts/prepare-restore-fixture.sh "$token"
backup_dir=$(sudo infra/wsl/scripts/backup.sh)
sudo infra/wsl/scripts/restore-verify.sh "$backup_dir" "$token"
```

Each backup contains a custom-format PostgreSQL dump, Redis RDB snapshot, compressed artifact and
checkpoint data, Git revision metadata, and SHA-256 checksums.
