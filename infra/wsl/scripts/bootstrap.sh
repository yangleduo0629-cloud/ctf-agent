#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
readonly UV_VERSION="0.8.3"
readonly PNPM_VERSION="10.13.1"
readonly KEYRING_DIR="/etc/apt/keyrings"

proxy_url="${HTTPS_PROXY:-${https_proxy:-}}"
if [[ -n ${proxy_url} ]]; then
  install -d -m 0755 /etc/systemd/system/docker.service.d
  cat > /etc/systemd/system/docker.service.d/proxy.conf <<EOF
[Service]
Environment="HTTP_PROXY=${proxy_url}"
Environment="HTTPS_PROXY=${proxy_url}"
Environment="NO_PROXY=localhost,127.0.0.1,::1,postgres,redis,api,web,litellm"
EOF
  systemctl daemon-reload
fi

install -d -m 0755 "${KEYRING_DIR}"
apt-get update
apt-get install -y --no-install-recommends \
  apt-transport-https build-essential ca-certificates curl git gnupg jq \
  libpq-dev openssl pkg-config postgresql-client redis-tools shellcheck unzip

curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
  | gpg --dearmor --yes -o "${KEYRING_DIR}/nodesource.gpg"
echo "deb [arch=$(dpkg --print-architecture) signed-by=${KEYRING_DIR}/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
  > /etc/apt/sources.list.d/nodesource.list

curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor --yes -o "${KEYRING_DIR}/docker.gpg"
chmod a+r "${KEYRING_DIR}/docker.gpg"
# shellcheck disable=SC1091
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=${KEYRING_DIR}/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list

curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | gpg --dearmor --yes -o "${KEYRING_DIR}/nvidia-container-toolkit.gpg"
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed "s#deb https://#deb [signed-by=${KEYRING_DIR}/nvidia-container-toolkit.gpg] https://#g" \
  > /etc/apt/sources.list.d/nvidia-container-toolkit.list

apt-get update
apt-get install -y --no-install-recommends \
  containerd.io docker-buildx-plugin docker-ce docker-ce-cli docker-compose-plugin \
  nodejs nvidia-container-toolkit

curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" \
  | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
npm install --global "pnpm@${PNPM_VERSION}"

systemctl enable --now docker
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker

getent group docker >/dev/null || groupadd --system docker
usermod -aG docker ctf
usermod -aG docker ctf-platform

install -d -o ctf-platform -g ctf-platform -m 2770 \
  /srv/ctf-platform/app \
  /srv/ctf-platform/data/artifacts \
  /srv/ctf-platform/data/checkpoints \
  /srv/ctf-platform/models
install -d -o 999 -g 999 -m 0750 \
  /srv/ctf-platform/data/postgres \
  /srv/ctf-platform/data/redis
install -d -m 0711 /var/lib/docker

echo "Installed versions:"
git --version
curl --version | head -n 1
uv --version
node --version
pnpm --version
docker --version
docker compose version
nvidia-ctk --version
findmnt -T /var/lib/docker -no TARGET,SOURCE,FSTYPE
