#!/usr/bin/env sh
set -eu

# Curlable installer. Override INCIDENT_HARNESS_SOURCE for a fork or a release URL.
SOURCE=${INCIDENT_HARNESS_SOURCE:-git+https://github.com/Isaac12x/incident-resolver-agent.git}
PACKAGE=${INCIDENT_HARNESS_PACKAGE:-incident-harness}

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' 'uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/.' >&2
  exit 1
fi

uv tool install --force --from "$SOURCE" "$PACKAGE"
printf '%s\n' "Installed $PACKAGE in uv's isolated tool environment."
printf '%s\n' 'Run: incident-agent init && incident-agent config'
