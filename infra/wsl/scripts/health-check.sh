#!/usr/bin/env bash
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly INFRA_DIR
readonly ENV_FILE="${INFRA_DIR}/.env"
readonly COMPOSE=(docker compose --env-file "${ENV_FILE}" -f "${INFRA_DIR}/compose.yaml")
readonly GPU_IMAGE="${GPU_TEST_IMAGE:-nvidia/cuda:12.8.1-base-ubuntu24.04}"
skip_gpu=false
[[ ${1:-} == "--skip-gpu" ]] && skip_gpu=true

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

"${COMPOSE[@]}" ps
curl --fail --silent --show-error http://127.0.0.1:8080/readyz | jq -e '
  .status == "ready"
  and .dependencies.postgres
  and .dependencies.redis
  and .dependencies.sandbox_runner
  and .dependencies.schema_revision == "20260721_0003"
  and .storage.artifacts
  and .storage.checkpoints
  and .storage.sandboxes' >/dev/null
curl --fail --silent --show-error http://127.0.0.1:3000/ >/dev/null
"${COMPOSE[@]}" exec -T postgres pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" >/dev/null
"${COMPOSE[@]}" exec -T redis redis-cli --no-auth-warning -a "${REDIS_PASSWORD}" ping | grep -q PONG
"${COMPOSE[@]}" exec -T worker python -m backend.orchestration.worker --healthcheck
"${COMPOSE[@]}" exec -T litellm python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4000/health/liveliness', timeout=5)"

for path in /srv/ctf-platform/app /srv/ctf-platform/data /srv/ctf-platform/models /var/lib/docker; do
  read -r source fs_type < <(findmnt -T "${path}" -no SOURCE,FSTYPE)
  [[ ${fs_type} == "ext4" && ${source} != /mnt/* ]]
done
[[ $(docker info --format '{{.DockerRootDir}}') == "/var/lib/docker" ]]
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader

if [[ ${skip_gpu} == false ]]; then
  docker run --rm --gpus all "${GPU_IMAGE}" \
    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
fi

echo "All requested service and storage health checks passed."
