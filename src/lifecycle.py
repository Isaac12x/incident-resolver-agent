"""Installation and runtime lifecycle helpers for the ``incident-agent`` CLI."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

PACKAGE_NAME = "incident-harness"


def default_config_path(cwd: Path | None = None) -> Path:
    """Return a stable config path, preferring an explicit checkout config."""
    configured = os.environ.get("INCIDENT_AGENT_CONFIG")
    if configured:
        return Path(configured).expanduser()
    root = (cwd or Path.cwd()).resolve()
    local = root / ".agent" / "config.toml"
    if local.exists() or (root / ".agent").is_dir():
        return local
    config_root = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return config_root / PACKAGE_NAME / "config.toml"


def default_runtime_path() -> Path:
    """Return the persistent per-user state directory used by installed runs."""
    state_root = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser()
    return state_root / PACKAGE_NAME


def bootstrap(config_path: Path) -> Path:
    """Create a valid config parent and return the resolved config path.

    Runtime directories are created by ``Storage``; keeping this operation small makes
    it safe to invoke before every command, including after an interrupted install.
    """
    path = config_path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def ensure_user_config(path: Path) -> None:
    """Create the installed user's config with state outside the config directory."""
    if path.exists():
        return
    from .config import ConnectorConfig, load_config, save_config

    config = load_config(path)
    config.runtime_root = default_runtime_path()
    # Installed intake should require the configured API boundary by default.
    # Keep this opt-in only for newly created user configuration; existing files
    # are never silently changed by an upgrade.
    config.server.require_api_auth = True
    if not config.connectors:
        config.connectors.append(
            ConnectorConfig(name="grafana", purpose="incident", type="webhook")
        )
    save_config(config, path)


def update_installation(*, runner=subprocess.run) -> subprocess.CompletedProcess[str]:
    """Update the isolated uv tool installation and return the real command result."""
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError(
            "uv is required for updates; install it from https://docs.astral.sh/uv/"
        )
    return runner(
        [uv, "tool", "upgrade", PACKAGE_NAME],
        capture_output=True,
        text=True,
        check=False,
    )
