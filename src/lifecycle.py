"""Installation and runtime lifecycle helpers for the ``incident-agent`` CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from importlib.metadata import distribution
from pathlib import Path
from urllib.parse import urlparse

PACKAGE_NAME = "incident-harness"
DEFAULT_RELEASE_REPOSITORY = "Isaac12x/incident-resolver-agent"


def release_asset_url(
    *,
    repository: str = DEFAULT_RELEASE_REPOSITORY,
    version: str = "latest",
    asset: str | None = None,
    opener=urllib.request.urlopen,
) -> str:
    """Resolve a valid versioned wheel URL from a GitHub release."""
    endpoint = f"https://api.github.com/repos/{repository}/releases/{version}"
    if version != "latest":
        endpoint = f"https://api.github.com/repos/{repository}/releases/tags/{version}"
    try:
        with opener(endpoint, timeout=10) as response:
            release = json.load(response)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"could not resolve release metadata from {endpoint}: {error}"
        ) from error
    candidates = [
        item for item in release.get("assets", []) if item.get("name", "").endswith(".whl")
    ]
    if asset:
        candidates = [item for item in candidates if item.get("name") == asset]
    else:
        candidates = [
            item
            for item in candidates
            if item.get("name", "").startswith("incident_harness-")
        ]
    if not candidates or not candidates[0].get("browser_download_url"):
        requested = asset or "incident_harness wheel"
        raise RuntimeError(f"release {repository}@{version} has no {requested} asset")
    return candidates[0]["browser_download_url"]


def installation_source() -> str:
    """Return the explicit source or current valid release wheel URL."""
    source = os.environ.get("INCIDENT_HARNESS_SOURCE")
    if source:
        return source
    repository = os.environ.get("INCIDENT_HARNESS_REPOSITORY", DEFAULT_RELEASE_REPOSITORY)
    version = os.environ.get("INCIDENT_HARNESS_VERSION", "latest")
    asset = os.environ.get("INCIDENT_HARNESS_RELEASE_ASSET")
    return release_asset_url(repository=repository, version=version, asset=asset)


def installed_release_repository() -> str | None:
    """Return the GitHub repository recorded for a release wheel install."""
    try:
        metadata = distribution(PACKAGE_NAME).read_text("direct_url.json")
    except Exception:
        return None
    if not metadata:
        return None
    try:
        url = json.loads(metadata).get("url", "")
        parsed = urlparse(url)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.hostname != "github.com" or len(parts) < 5:
        return None
    if parts[2] != "releases" or parts[3] not in {"download", "latest"}:
        return None
    return "/".join(parts[:2])


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    message: str


def doctor(config_path: Path | str, *, runner=subprocess.run) -> list[Check]:
    """Perform bounded, read-only readiness checks for a configured installation."""
    config_path = Path(config_path).expanduser().resolve()
    from dotenv import load_dotenv

    load_dotenv(config_path.parent / ".env", override=False)
    from .bundles import effective_config
    from .config import load_config
    from .tooling import probe_subscription_cli

    if not config_path.is_file():
        return [Check("config", False, f"configuration file is missing: {config_path}")]
    try:
        config = load_config(config_path, create=False)
        config = effective_config(config)
    except Exception as error:
        return [Check("config", False, f"configuration is invalid: {error}")]
    checks = [Check("config", True, str(config_path))]
    if config.execution.mode == "container":
        docker = shutil.which("docker")
        if not docker:
            checks.append(Check("container runtime", False, "docker executable is not installed"))
        else:
            image = runner(
                [docker, "image", "inspect", config.execution.image],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            checks.append(
                Check(
                    "container image",
                    image.returncode == 0,
                    config.execution.image
                    if image.returncode == 0
                    else f"image is unavailable: {config.execution.image}",
                )
            )
    if config.model.runtime == "subscription-cli":
        probe = probe_subscription_cli(
            config.model.subscription_command, config.model.subscription_profile, runner=runner
        )
        checks.append(Check("model runtime", probe.ready, probe.message))
    else:
        checks.append(Check("model runtime", True, f"{config.model.provider}/{config.model.name}"))
        if (
            config.model.mode == "remote"
            and config.model.api_key_env
            and not os.environ.get(config.model.api_key_env)
        ):
            checks.append(
                Check(
                    "model credentials",
                    False,
                    f"environment variable is not set: {config.model.api_key_env}",
                )
            )
    if config.server.require_api_auth and not os.environ.get(config.server.api_token_env):
        checks.append(
            Check(
                "API authentication",
                False,
                f"environment variable is not set: {config.server.api_token_env}",
            )
        )
    webhook_configured = any(connector.type == "webhook" for connector in config.connectors)
    if webhook_configured and not os.environ.get(config.server.webhook_secret_env):
        checks.append(
            Check(
                "webhook authentication",
                False,
                f"environment variable is not set: {config.server.webhook_secret_env}",
            )
        )
    for connector in config.connectors:
        if connector.type == "mcp" and connector.transport == "stdio" and connector.command:
            executable = shutil.which(connector.command[0])
            checks.append(
                Check(
                    f"connector {connector.name} executable",
                    executable is not None,
                    executable or f"command not found: {connector.command[0]}",
                )
            )
        if connector.auth_token_env and not os.environ.get(connector.auth_token_env):
            checks.append(
                Check(
                    f"connector {connector.name}",
                    False,
                    f"environment variable is not set: {connector.auth_token_env}",
                )
            )
    for repository in config.repositories:
        location = repository.local_path
        ready = bool(location and Path(location).is_dir()) or bool(repository.clone_url)
        checks.append(
            Check(
                f"repository {repository.name}",
                ready,
                str(location or repository.clone_url or "repository source is not configured"),
            )
        )
    return checks


def require_ready(config_path: Path | str) -> None:
    failures = [check for check in doctor(config_path) if not check.ok]
    if failures:
        details = "\n".join(f"- {check.name}: {check.message}" for check in failures)
        raise RuntimeError("incident-agent is not ready; run 'incident-agent doctor'\n" + details)


def ensure_runtime_tools(config_path: Path | str) -> None:
    """Install missing managed graph tools when the saved permission allows it."""
    from .config import load_config
    from .tooling import executable_status, install_uv_tools

    config = load_config(Path(config_path), create=False)
    required = ("seed", "code-review-graph") if config.repositories else ()
    missing = [name for name, available in executable_status(required).items() if not available]
    if not missing:
        return
    if not config.permissions.allow_dependency_installation:
        raise RuntimeError(
            "required tools are missing and dependency installation is disabled: "
            + ", ".join(missing)
        )
    results = install_uv_tools(missing)
    failures = [result for result in results if not result.succeeded]
    if failures:
        raise RuntimeError(
            "managed tool installation failed: "
            + "; ".join(
                (result.stderr or result.stdout or str(result.command)).strip()
                for result in failures
            )
        )


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


def update_installation(
    *, runner=subprocess.run, managed_tools: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    """Update the isolated uv tool installation and return the real command result."""
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required for updates; install it from https://docs.astral.sh/uv/")
    explicit_source = any(
        os.environ.get(name)
        for name in (
            "INCIDENT_HARNESS_SOURCE",
            "INCIDENT_HARNESS_REPOSITORY",
            "INCIDENT_HARNESS_VERSION",
            "INCIDENT_HARNESS_RELEASE_ASSET",
        )
    )
    if explicit_source:
        command = [uv, "tool", "install", "--force", "--from", installation_source(), PACKAGE_NAME]
    elif repository := installed_release_repository():
        command = [
            uv,
            "tool",
            "install",
            "--force",
            "--from",
            release_asset_url(repository=repository),
            PACKAGE_NAME,
        ]
    else:
        command = [uv, "tool", "upgrade", PACKAGE_NAME]
    result = runner(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return result
    from .tooling import TOOL_PACKAGES

    for tool in managed_tools:
        upgraded = runner(
            [uv, "tool", "install", "--upgrade", TOOL_PACKAGES.get(tool, tool)],
            capture_output=True,
            text=True,
            check=False,
        )
        if upgraded.returncode:
            return upgraded
    return result
