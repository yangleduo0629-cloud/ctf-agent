#!/usr/bin/env bash
set -euo pipefail

readonly BACKUP_DIR="${1:?usage: restore-verify.sh BACKUP_DIR TOKEN}"
readonly TOKEN="${2:?usage: restore-verify.sh BACKUP_DIR TOKEN}"
readonly INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ENV_FILE="${INFRA_DIR}/.env"
readonly COMPOSE=(docker compose --env-file "${ENV_FILE}" -f "${INFRA_DIR}/compose.yaml")
readonly RESTORE_DB="ctf_restore_verify"
readonly REDIS_CONTAINER="ctf-redis-restore-verify"

case "$(realpath "${BACKUP_DIR}")" in
  /mnt/f/CTF-Agent-Backups/*) ;;
  *) echo "Backup must be below /mnt/f/CTF-Agent-Backups." >&2; exit 1 ;;
esac
[[ -f ${BACKUP_DIR}/backup.complete ]]

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

tmp_dir=$(mktemp -d)
cleanup() {
  docker rm --force "${REDIS_CONTAINER}" >/dev/null 2>&1 || true
  "${COMPOSE[@]}" exec -T postgres dropdb --if-exists -U "${POSTGRES_USER}" "${RESTORE_DB}" >/dev/null 2>&1 || true
  rm -rf "${tmp_dir}"
}
trap cleanup EXIT

(cd "${BACKUP_DIR}" && sha256sum --check SHA256SUMS)
"${COMPOSE[@]}" exec -T postgres dropdb --if-exists -U "${POSTGRES_USER}" "${RESTORE_DB}" >/dev/null
"${COMPOSE[@]}" exec -T postgres createdb -U "${POSTGRES_USER}" "${RESTORE_DB}"
"${COMPOSE[@]}" exec -T postgres pg_restore \
  -U "${POSTGRES_USER}" -d "${RESTORE_DB}" < "${BACKUP_DIR}/postgres/ctf_platform.dump"
postgres_value=$("${COMPOSE[@]}" exec -T postgres psql -At \
  -U "${POSTGRES_USER}" -d "${RESTORE_DB}" \
  -c "SELECT probe_value FROM infra_restore_probe WHERE probe_key = 'wsl-bootstrap'")
[[ ${postgres_value} == "${TOKEN}" ]]

install -d "${tmp_dir}/redis"
cp "${BACKUP_DIR}/redis/dump.rdb" "${tmp_dir}/redis/dump.rdb"
chmod 0777 "${tmp_dir}/redis"
chmod 0666 "${tmp_dir}/redis/dump.rdb"
docker run --detach --rm --name "${REDIS_CONTAINER}" \
  --volume "${tmp_dir}/redis:/data" redis:7.4.5-bookworm \
  redis-server --appendonly no >/dev/null
for _ in $(seq 1 20); do
  docker exec "${REDIS_CONTAINER}" redis-cli ping 2>/dev/null | grep -q PONG && break
  sleep 1
done
redis_value=$(docker exec "${REDIS_CONTAINER}" redis-cli GET infra:restore:probe)
[[ ${redis_value} == "${TOKEN}" ]]

tar --extract --gzip --file "${BACKUP_DIR}/files/platform-data.tar.gz" -C "${tmp_dir}"
[[ $(< "${tmp_dir}/artifacts/restore-probe.txt") == "${TOKEN}" ]]
[[ $(< "${tmp_dir}/checkpoints/restore-probe.txt") == "${TOKEN}" ]]
echo "Backup restore verification passed for PostgreSQL, Redis, artifacts, and checkpoints."
