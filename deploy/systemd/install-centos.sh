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
VENV_BIN="${INSTALL_ROOT}/.venv/bin"
SERVICE_LAYOUT="${INCIDENT_HARNESS_LAYOUT:-combined}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

if [[ "${SERVICE_LAYOUT}" != "combined" && "${SERVICE_LAYOUT}" != "split" ]]; then
  echo "INCIDENT_HARNESS_LAYOUT must be 'combined' or 'split'." >&2
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

echo "==> Syncing application to ${INSTALL_ROOT}"
systemctl disable --now incident-harness.service incident-harness.target \
  incident-harness-http.service incident-harness-worker.service >/dev/null 2>&1 || true
install -d -m 0755 "${INSTALL_ROOT}"
rsync -a --delete \
  --exclude .agent \
  --exclude .venv \
  --exclude .git \
  "${REPO_ROOT}/" "${INSTALL_ROOT}/"
prepare_owned_dir "${INSTALL_ROOT}/.agent"

# Runtime state is intentionally excluded from rsync, but config.toml contains
# only settings and environment-variable names. Seed a fresh installation from
# the checkout's configured file when one is available; never overwrite a
# non-empty deployed configuration during an upgrade.
if [[ ! -s "${INSTALL_ROOT}/.agent/config.toml" ]] \
  && [[ -s "${REPO_ROOT}/.agent/config.toml" ]] \
  && [[ "${REPO_ROOT}/.agent/config.toml" != "${INSTALL_ROOT}/.agent/config.toml" ]]; then
  install -m 0640 -o "${SERVICE_USER}" -g "${SERVICE_USER}" \
    "${REPO_ROOT}/.agent/config.toml" "${INSTALL_ROOT}/.agent/config.toml"
fi

echo "==> Creating virtualenv, syncing dependencies, and initializing .agent"
install_uv_if_needed
(
  cd "${INSTALL_ROOT}"
  export PATH="${VENV_BIN}:${PATH}"
  "${UV_BIN}" sync
  if [[ ! -x "${VENV_BIN}/seed" ]]; then
    echo "Missing ${VENV_BIN}/seed after uv sync." >&2
    exit 1
  fi
  "${VENV_BIN}/incident-agent" init
  "${VENV_BIN}/incident-agent" install-repositories \
    --source-root "${REPO_ROOT}/.agent/repositories" \
    --destination-root "${INSTALL_ROOT}/.agent/repositories"
)
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_ROOT}"

if (cd "${INSTALL_ROOT}" && "${VENV_BIN}/python" -c '
from src.config import load_config
import sys
sys.exit(0 if any(r.publish_mode == "github" or "github.com" in (r.clone_url or "")
                  for r in load_config().repositories) else 1)
'); then
if ! command -v gh >/dev/null 2>&1; then
  echo "GitHub CLI (gh) is required for repository login and PR publishing." >&2
  exit 1
fi

# The TUI login belongs to the setup account. Provision that same GitHub account
# for the service on first install, without copying an entire home or printing a token.
# Preserve an existing service login on upgrades. Pipe credentials over stdin only.
(
  cd "${INSTALL_ROOT}"
  service_gh() {
    runuser -u "${SERVICE_USER}" -- env -u GH_TOKEN -u GITHUB_TOKEN \
      -u GH_CONFIG_DIR -u XDG_CONFIG_HOME gh "$@"
  }
  if ! service_gh auth status --hostname github.com >/dev/null 2>&1; then
    if gh auth status --hostname github.com >/dev/null 2>&1; then
      echo "==> Provisioning the TUI GitHub account for ${SERVICE_USER}"
      gh auth token --hostname github.com | \
        service_gh auth login --hostname github.com --git-protocol https --with-token
    else
      echo "GitHub login required: run gh auth login in the TUI setup account or as ${SERVICE_USER}." >&2
      exit 1
    fi
  fi
  service_gh auth setup-git --hostname github.com
  # Existing selections can contain SSH URLs. Use the same gh credential for Git and API calls.
  runuser -u "${SERVICE_USER}" -- git config --global --replace-all \
    url.https://github.com/.insteadOf git@github.com:
  runuser -u "${SERVICE_USER}" -- git config --global --add \
    url.https://github.com/.insteadOf ssh://git@github.com/
)

fi

echo "==> Installing systemd units and environment template"
install -d -m 0750 -o root -g "${SERVICE_USER}" "${ENV_DIR}"
if [[ ! -f "${ENV_DIR}/environment" ]]; then
  install -m 0640 -o root -g "${SERVICE_USER}" \
    "${SCRIPT_DIR}/incident-harness.env.example" "${ENV_DIR}/environment"
fi
chown root:"${SERVICE_USER}" "${ENV_DIR}/environment"
chmod 0640 "${ENV_DIR}/environment"

install -m 0644 "${SCRIPT_DIR}/incident-harness.service" "${UNIT_DIR}/"
install -m 0644 "${SCRIPT_DIR}/incident-harness-http.service" "${UNIT_DIR}/"
install -m 0644 "${SCRIPT_DIR}/incident-harness-worker.service" "${UNIT_DIR}/"
install -m 0644 "${SCRIPT_DIR}/incident-harness.target" "${UNIT_DIR}/"

systemctl daemon-reload

echo "==> Starting ${SERVICE_LAYOUT} service layout"
if [[ "${SERVICE_LAYOUT}" == "split" ]]; then
  systemctl disable --now incident-harness.service >/dev/null 2>&1 || true
  systemctl enable --now incident-harness.target
  ACTIVE_UNIT="incident-harness.target"
else
  systemctl disable --now incident-harness.target incident-harness-http.service \
    incident-harness-worker.service >/dev/null 2>&1 || true
  systemctl enable --now incident-harness.service
  ACTIVE_UNIT="incident-harness.service"
fi

runuser -u "${SERVICE_USER}" -w "${INSTALL_ROOT}" -- \
  "${VENV_BIN}/incident-agent" healthcheck --timeout 30

cat <<EOF
Installed and running (${ACTIVE_UNIT}).

Configuration:
  1. Edit secrets when required:
       ${ENV_DIR}/environment
  2. Adjust the generated or copied configuration when required:
       runuser -u ${SERVICE_USER} -w ${INSTALL_ROOT} -- \\
         ${INSTALL_ROOT}/.venv/bin/incident-agent tui
  3. Restart after configuration or authentication changes:
       systemctl restart ${ACTIVE_UNIT}
  4. Verify intake readiness:
       systemctl status ${ACTIVE_UNIT}
       curl -s "\$(runuser -u ${SERVICE_USER} -w ${INSTALL_ROOT} -- \\
         ${INSTALL_ROOT}/.venv/bin/incident-agent service-url)/health"

Grafana intake URL (default port 8765):
  http://HOST:8765/hooks/incidents/grafana

To install the mutually exclusive split HTTP/worker layout instead:
  INCIDENT_HARNESS_LAYOUT=split ${SCRIPT_DIR}/install-centos.sh

Subscription CLI (codex, etc.): authenticate as the service user before intake:
  runuser -u ${SERVICE_USER} -- env HOME=${SERVICE_HOME} codex login
EOF
