#!/usr/bin/env bash
set -euo pipefail

readonly INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ENV_FILE="${INFRA_DIR}/.env"
readonly COMPOSE=(docker compose --env-file "${ENV_FILE}" -f "${INFRA_DIR}/compose.yaml")
readonly BACKUP_ROOT="${BACKUP_ROOT:-/mnt/f/CTF-Agent-Backups/ctf-platform}"
readonly STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly DESTINATION="${BACKUP_ROOT}/${STAMP}"

case "${BACKUP_ROOT}" in
  /mnt/f/CTF-Agent-Backups|/mnt/f/CTF-Agent-Backups/*) ;;
  *) echo "Backup root must stay below /mnt/f/CTF-Agent-Backups." >&2; exit 1 ;;
esac

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

install -d "${DESTINATION}/postgres" "${DESTINATION}/redis" "${DESTINATION}/files"
"${COMPOSE[@]}" exec -T postgres pg_dump \
  -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" --format=custom \
  > "${DESTINATION}/postgres/ctf_platform.dump"
"${COMPOSE[@]}" exec -T redis redis-cli --no-auth-warning -a "${REDIS_PASSWORD}" SAVE >/dev/null
redis_id=$("${COMPOSE[@]}" ps -q redis)
docker cp "${redis_id}:/data/dump.rdb" "${DESTINATION}/redis/dump.rdb" >/dev/null
tar --create --gzip --file "${DESTINATION}/files/platform-data.tar.gz" \
  -C /srv/ctf-platform/data artifacts checkpoints

jq -n \
  --arg created_at "${STAMP}" \
  --arg git_commit "$(git -C /srv/ctf-platform/app rev-parse HEAD)" \
  --arg distribution "Ubuntu-24.04" \
  '{schema_version: 1, created_at: $created_at, git_commit: $git_commit, distribution: $distribution}' \
  > "${DESTINATION}/metadata.json"
(
  cd "${DESTINATION}"
  find . -type f ! -name SHA256SUMS -print0 \
    | sort -z \
    | xargs -0 sha256sum > SHA256SUMS
)
touch "${DESTINATION}/backup.complete"
echo "${DESTINATION}"
