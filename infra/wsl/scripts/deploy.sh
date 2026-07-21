#!/usr/bin/env bash
set -euo pipefail

readonly INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ENV_FILE="${INFRA_DIR}/.env"
readonly COMPOSE_FILE="${INFRA_DIR}/compose.yaml"

for path in /srv/ctf-platform/app /srv/ctf-platform/data /srv/ctf-platform/models /var/lib/docker; do
  fs_type=$(findmnt -T "${path}" -no FSTYPE)
  source=$(findmnt -T "${path}" -no SOURCE)
  if [[ ${fs_type} != "ext4" || ${source} == /mnt/* ]]; then
    echo "Active path is outside WSL ext4: ${path} (${source} ${fs_type})" >&2
    exit 1
  fi
done

if [[ ! -f ${ENV_FILE} ]]; then
  umask 077
  postgres_password=$(openssl rand -hex 24)
  redis_password=$(openssl rand -hex 24)
  litellm_key=$(openssl rand -hex 24)
  cat > "${ENV_FILE}" <<EOF
COMPOSE_PROJECT_NAME=ctf-platform
POSTGRES_DB=ctf_platform
POSTGRES_USER=ctf_platform
POSTGRES_PASSWORD=${postgres_password}
REDIS_PASSWORD=${redis_password}
LITELLM_MASTER_KEY=sk-${litellm_key}
LOCAL_MODEL_API_BASE=http://host.docker.internal:8001/v1
LOCAL_MODEL_API_KEY=local-model-placeholder
EOF
fi
chmod 0600 "${ENV_FILE}"

install -d -o 999 -g 999 -m 0750 \
  /srv/ctf-platform/data/postgres \
  /srv/ctf-platform/data/redis
install -d -o ctf-platform -g ctf-platform -m 2770 \
  /srv/ctf-platform/data/artifacts \
  /srv/ctf-platform/data/checkpoints \
  /srv/ctf-platform/models

docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" config --quiet
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" build --pull
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up --detach --remove-orphans

for _ in $(seq 1 60); do
  unhealthy=$(docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ps \
    --format json | jq -s '[.[] | select(.Health != "healthy")] | length')
  running=$(docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ps \
    --status running --services | wc -l)
  if [[ ${running} -eq 5 && ${unhealthy} -eq 0 ]]; then
    exec "${INFRA_DIR}/scripts/health-check.sh" --skip-gpu
  fi
  sleep 5
done

docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ps
echo "Service stack did not become healthy before the timeout." >&2
exit 1
