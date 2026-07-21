#!/usr/bin/env bash
set -euo pipefail

readonly TOKEN="${1:?usage: verify-fixture.sh TOKEN}"
readonly INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ENV_FILE="${INFRA_DIR}/.env"
readonly COMPOSE=(docker compose --env-file "${ENV_FILE}" -f "${INFRA_DIR}/compose.yaml")

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

postgres_value=$("${COMPOSE[@]}" exec -T postgres psql -At \
  -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
  -c "SELECT probe_value FROM infra_restore_probe WHERE probe_key = 'wsl-bootstrap'")
redis_value=$("${COMPOSE[@]}" exec -T redis redis-cli --no-auth-warning -a "${REDIS_PASSWORD}" \
  GET infra:restore:probe)

[[ ${postgres_value} == "${TOKEN}" ]]
[[ ${redis_value} == "${TOKEN}" ]]
[[ $(< /srv/ctf-platform/data/artifacts/restore-probe.txt) == "${TOKEN}" ]]
[[ $(< /srv/ctf-platform/data/checkpoints/restore-probe.txt) == "${TOKEN}" ]]
echo "Persistence fixture ${TOKEN} is intact."
