#!/usr/bin/env bash
# Install Incident Harness as a systemd service on CentOS/RHEL 7+.
# Run as root from the repository checkout:
#   ./deploy/systemd/install-centos.sh
set -euo pipefail

INSTALL_ROOT="${INSTALL_ROOT:-/opt/incident-harness}"
SERVICE_USER="incident-harness"
SERVICE_HOME="/var/lib/incident-harness"
ENV_DIR="/etc/incident-harness"
UNIT_DIR="/etc/systemd/system"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
UV_BIN="$(command -v uv)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  echo "git is required." >&2
  exit 1
fi

if [[ -z "${UV_BIN}" ]]; then
  echo "uv is required. Install from https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

prepare_owned_dir() {
  local path="$1"
  local mode="${2:-0750}"
  install -d -m "${mode}" "${path}"
  chown "${SERVICE_USER}:${SERVICE_USER}" "${path}"
}

ensure_service_user() {
  if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "${SERVICE_HOME}" --shell /sbin/nologin "${SERVICE_USER}"
    return
  fi

  local current_home
  current_home="$(getent passwd "${SERVICE_USER}" | cut -d: -f6)"
  if [[ "${current_home}" != "${SERVICE_HOME}" ]]; then
    usermod -d "${SERVICE_HOME}" "${SERVICE_USER}"
  fi
}

install_uv_if_needed() {
  if [[ "${UV_BIN}" == /root/* ]] && [[ ! -x /usr/local/bin/uv ]]; then
    echo "==> Installing uv to /usr/local/bin (root-only install is not usable by ${SERVICE_USER})"
    install -m 0755 "${UV_BIN}" /usr/local/bin/uv
    UV_BIN="/usr/local/bin/uv"
  fi
}

echo "==> Ensuring service user and home (${SERVICE_HOME})"
ensure_service_user
prepare_owned_dir "${SERVICE_HOME}"
prepare_owned_dir "${SERVICE_HOME}/.ssh"
prepare_owned_dir "${SERVICE_HOME}/.cache"

echo "==> Installing application to ${INSTALL_ROOT}"
install -d -m 0755 "${INSTALL_ROOT}"
if [[ ! -f "${INSTALL_ROOT}/pyproject.toml" ]]; then
  rsync -a --delete \
    --exclude .agent \
    --exclude .venv \
    --exclude .git \
    "${REPO_ROOT}/" "${INSTALL_ROOT}/"
fi
prepare_owned_dir "${INSTALL_ROOT}/.agent"

echo "==> Creating virtualenv and syncing dependencies"
install_uv_if_needed
(
  cd "${INSTALL_ROOT}"
  "${UV_BIN}" sync
  "${INSTALL_ROOT}/.venv/bin/incident-agent" init
)
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_ROOT}"

echo "==> Installing systemd units and environment template"
install -d -m 0750 "${ENV_DIR}"
if [[ ! -f "${ENV_DIR}/environment" ]]; then
  install -m 0640 -o root -g "${SERVICE_USER}" \
    "${SCRIPT_DIR}/incident-harness.env.example" "${ENV_DIR}/environment"
fi

install -m 0644 "${SCRIPT_DIR}/incident-harness.service" "${UNIT_DIR}/"
install -m 0644 "${SCRIPT_DIR}/incident-harness-http.service" "${UNIT_DIR}/"
install -m 0644 "${SCRIPT_DIR}/incident-harness-worker.service" "${UNIT_DIR}/"
install -m 0644 "${SCRIPT_DIR}/incident-harness.target" "${UNIT_DIR}/"

systemctl daemon-reload

cat <<EOF
Installed.

Next steps:
  1. Edit secrets (if not done yet):
       ${ENV_DIR}/environment
  2. Configure the harness:
       runuser -u ${SERVICE_USER} -w ${INSTALL_ROOT} -- \\
         ${INSTALL_ROOT}/.venv/bin/incident-agent tui
  3. Start the service:
       systemctl enable --now incident-harness.service
  4. Check status:
       systemctl status incident-harness.service
       curl -s "\$(runuser -u ${SERVICE_USER} -w ${INSTALL_ROOT} -- \\
         ${INSTALL_ROOT}/.venv/bin/incident-agent service-url)/health"

Optional split HTTP/worker layout:
  systemctl enable --now incident-harness.target

Subscription CLI (codex, etc.): authenticate as the service user before intake:
  runuser -u ${SERVICE_USER} -- env HOME=${SERVICE_HOME} codex login
EOF
