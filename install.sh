#!/usr/bin/env sh
set -eu

# Curlable installer. By default this installs the wheel published on the latest
# GitHub release. Override INCIDENT_HARNESS_SOURCE for a fork, Git revision, or
# directly hosted wheel. INCIDENT_HARNESS_VERSION may be a tag such as v0.2.0.
REPOSITORY=${INCIDENT_HARNESS_REPOSITORY:-Isaac12x/incident-resolver-agent}
VERSION=${INCIDENT_HARNESS_VERSION:-latest}
ASSET=${INCIDENT_HARNESS_RELEASE_ASSET:-}
PACKAGE=${INCIDENT_HARNESS_PACKAGE:-incident-harness}

if [ -n "${INCIDENT_HARNESS_SOURCE:-}" ]; then
  SOURCE=$INCIDENT_HARNESS_SOURCE
fi

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' 'uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/.' >&2
  exit 1
fi

if [ -z "${INCIDENT_HARNESS_SOURCE:-}" ]; then
  if ! command -v curl >/dev/null 2>&1; then
    printf '%s\n' 'curl is required to download the release wheel.' >&2
    exit 1
  fi
  temporary_directory=$(mktemp -d "${TMPDIR:-/tmp}/incident-harness.XXXXXX")
  trap 'rm -rf "$temporary_directory"' EXIT HUP INT TERM
  release_endpoint="https://api.github.com/repos/$REPOSITORY/releases/latest"
  if [ "$VERSION" != latest ]; then
    release_endpoint="https://api.github.com/repos/$REPOSITORY/releases/tags/$VERSION"
  fi
  release_json="$temporary_directory/release.json"
  if ! curl --connect-timeout 10 --max-time 120 -fsSL "$release_endpoint" -o "$release_json"; then
    printf '%s\n' "Could not resolve release metadata: $release_endpoint" >&2
    exit 1
  fi
  if [ -n "$ASSET" ]; then
    asset_pattern=$(printf '%s' "$ASSET" | sed 's/[.[\*^$\\]/\\&/g')
    asset_url=$(sed -n "s/.*\"browser_download_url\": \"\(https:[^\"]*\/$asset_pattern\)\".*/\1/p" "$release_json" | head -n 1)
  else
    asset_url=$(sed -n 's/.*"browser_download_url": "\(https:[^"]*\/incident_harness-[0-9][^"]*\.whl\)".*/\1/p' "$release_json" | head -n 1)
  fi
  if [ -z "$asset_url" ]; then
    printf '%s\n' 'Release metadata does not contain a wheel asset.' >&2
    exit 1
  fi
  ASSET=${asset_url##*/}
  wheel_path="$temporary_directory/$ASSET"
  SOURCE=$asset_url
  if ! curl --connect-timeout 10 --max-time 120 -fsSL "$SOURCE" -o "$wheel_path"; then
    printf '%s\n' "Could not download release asset: $SOURCE" >&2
    exit 1
  fi
  if [ -n "${INCIDENT_HARNESS_SHA256:-}" ]; then
    expected_checksum=$INCIDENT_HARNESS_SHA256
    if command -v sha256sum >/dev/null 2>&1; then
      actual_checksum=$(sha256sum "$wheel_path" | awk '{print $1}')
    elif command -v shasum >/dev/null 2>&1; then
      actual_checksum=$(shasum -a 256 "$wheel_path" | awk '{print $1}')
    else
      printf '%s\n' 'sha256sum or shasum is required to verify the release wheel.' >&2
      exit 1
    fi
    if [ "$actual_checksum" != "$expected_checksum" ]; then
      printf '%s\n' 'Release wheel checksum verification failed.' >&2
      exit 1
    fi
    SOURCE="$SOURCE#sha256=$expected_checksum"
  fi
fi

uv tool install --force --from "$SOURCE" "$PACKAGE"
printf '%s\n' "Installed $PACKAGE in uv's isolated tool environment."
printf '%s\n' 'Run: incident-agent init && incident-agent config'
