"""Generate systemd EnvironmentFile content from TUI config and secret stores."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from dotenv import dotenv_values

from .config import Config, load_config

PASSTHROUGH_ENV_VARS = (
    "GIT_SSH_COMMAND",
    "GIT_ASKPASS",
    "SSH_AUTH_SOCK",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
)


def referenced_env_vars(config: Config) -> frozenset[str]:
    """Return environment variable names referenced by the current TUI config."""
    names: set[str] = set()
    if config.model.api_key_env:
        names.add(config.model.api_key_env)
    if config.model.organization_env:
        names.add(config.model.organization_env)
    names.add(config.server.webhook_secret_env)
    names.add(config.github.webhook_secret_env)
    for connector in config.connectors:
        if connector.auth_token_env:
            names.add(connector.auth_token_env)
    for repository in config.repositories:
        if repository.playwright.base_url_env:
            names.add(repository.playwright.base_url_env)
    return frozenset(names)


def service_base_url(config: Config) -> str:
    """Return the local health-check URL derived from server settings in config."""
    if config.server.public_url:
        return config.server.public_url.rstrip("/")
    host = config.server.host
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{config.server.port}"


def _load_secret_store(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = dotenv_values(path)
    return {key: value for key, value in values.items() if key and value is not None}


def merge_secret_stores(paths: Sequence[Path]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for path in paths:
        for key, value in _load_secret_store(path).items():
            if value != "":
                merged[key] = value
    return merged


def build_systemd_environment(
    config: Config,
    secrets: Mapping[str, str],
) -> dict[str, str]:
    """Select env vars required by config plus operational passthrough values."""
    selected: dict[str, str] = {}
    for name in sorted(referenced_env_vars(config)):
        if name in secrets:
            selected[name] = secrets[name]
    for name in PASSTHROUGH_ENV_VARS:
        if name in secrets:
            selected[name] = secrets[name]
    return selected


def _format_env_value(value: str) -> str:
    if value == "":
        return '""'
    if re.fullmatch(r"[\w@/+.-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def format_systemd_environment(values: Mapping[str, str]) -> str:
    lines = [f"{key}={_format_env_value(value)}" for key, value in sorted(values.items())]
    return "\n".join(lines) + ("\n" if lines else "")


def export_systemd_environment(
    *,
    config_path: Path,
    output_path: Path,
    secrets_paths: Sequence[Path],
) -> dict[str, str]:
    config = load_config(config_path, create=False)
    secrets = merge_secret_stores(secrets_paths)
    environment = build_systemd_environment(config, secrets)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(format_systemd_environment(environment), encoding="utf-8")
    temporary.replace(output_path)
    return environment


def default_secrets_paths(working_directory: Path | None = None) -> list[Path]:
    root = (working_directory or Path.cwd()).resolve()
    return [
        Path("/etc/incident-harness/environment"),
        root / ".env",
    ]
