#!/usr/bin/env bash
set -euo pipefail

readonly TOKEN="${1:?usage: prepare-restore-fixture.sh TOKEN}"
INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly INFRA_DIR
readonly ENV_FILE="${INFRA_DIR}/.env"
readonly COMPOSE=(docker compose --env-file "${ENV_FILE}" -f "${INFRA_DIR}/compose.yaml")

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

"${COMPOSE[@]}" exec -T postgres psql -v ON_ERROR_STOP=1 \
  -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -v token="${TOKEN}" <<'SQL'
CREATE TABLE IF NOT EXISTS infra_restore_probe (
  probe_key text PRIMARY KEY,
  probe_value text NOT NULL
);
INSERT INTO infra_restore_probe (probe_key, probe_value)
VALUES ('wsl-bootstrap', :'token')
ON CONFLICT (probe_key) DO UPDATE SET probe_value = EXCLUDED.probe_value;
SQL
"${COMPOSE[@]}" exec -T redis redis-cli --no-auth-warning -a "${REDIS_PASSWORD}" \
  SET infra:restore:probe "${TOKEN}" >/dev/null
printf '%s\n' "${TOKEN}" > /srv/ctf-platform/data/artifacts/restore-probe.txt
printf '%s\n' "${TOKEN}" > /srv/ctf-platform/data/checkpoints/restore-probe.txt
sync
echo "Prepared persistence fixture ${TOKEN}."
