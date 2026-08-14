#!/usr/bin/env bash
# Install Incident Harness as a systemd service on CentOS/RHEL 7+.
set -euo pipefail

INSTALL_ROOT="${INSTALL_ROOT:-/opt/incident-harness}"
ENV_DIR="/etc/incident-harness"
UNIT_DIR="/etc/systemd/system"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  echo "git is required." >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install from https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

if ! id incident-harness >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/incident-harness --shell /sbin/nologin incident-harness
fi

install -d -m 0755 "${INSTALL_ROOT}"
if [[ ! -f "${INSTALL_ROOT}/pyproject.toml" ]]; then
  rsync -a --delete \
    --exclude .agent \
    --exclude .venv \
    --exclude .git \
    "${REPO_ROOT}/" "${INSTALL_ROOT}/"
fi

install -d -m 0750 -o incident-harness -g incident-harness "${INSTALL_ROOT}/.agent"
install -d -m 0750 -o incident-harness -g incident-harness /var/lib/incident-harness/.ssh

cd "${INSTALL_ROOT}"
sudo -u incident-harness uv sync

install -d -m 0750 "${ENV_DIR}"
if [[ ! -f "${ENV_DIR}/environment" ]]; then
  install -m 0640 -o root -g incident-harness \
    "${SCRIPT_DIR}/incident-harness.env.example" "${ENV_DIR}/environment"
  echo "Edit ${ENV_DIR}/environment before starting the service."
fi

install -m 0644 "${SCRIPT_DIR}/incident-harness.service" "${UNIT_DIR}/"
install -m 0644 "${SCRIPT_DIR}/incident-harness-http.service" "${UNIT_DIR}/"
install -m 0644 "${SCRIPT_DIR}/incident-harness-worker.service" "${UNIT_DIR}/"
install -m 0644 "${SCRIPT_DIR}/incident-harness.target" "${UNIT_DIR}/"

chown -R incident-harness:incident-harness "${INSTALL_ROOT}" /var/lib/incident-harness

systemctl daemon-reload

cat <<EOF
Installed.

Recommended (single unit — webhooks + worker):
  sudo systemctl enable --now incident-harness.service

Optional split (restart worker without dropping webhook intake):
  sudo systemctl enable --now incident-harness.target

Before production intake:
  1. Configure repositories, server host/port, and env var names via:
       sudo -u incident-harness ${INSTALL_ROOT}/.venv/bin/incident-agent tui
  2. Put secret values in ${ENV_DIR}/environment using the env var names from the TUI.
  3. Validate the generated runtime environment:
       sudo -u incident-harness ${INSTALL_ROOT}/.venv/bin/incident-agent export-systemd-env --output /tmp/incident-harness.env
  4. Set max_concurrent_tasks in .agent/config.toml for parallel incidents.
  5. Point GitHub/Sentry webhooks at:
       \$(sudo -u incident-harness ${INSTALL_ROOT}/.venv/bin/incident-agent service-url)/hooks/github
       \$(sudo -u incident-harness ${INSTALL_ROOT}/.venv/bin/incident-agent service-url)/hooks/incidents/<connector>

Status:
  systemctl status incident-harness.service
  curl -s "\$(sudo -u incident-harness ${INSTALL_ROOT}/.venv/bin/incident-agent service-url)/health"
EOF
