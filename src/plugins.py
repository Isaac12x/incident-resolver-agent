"""Discoverable source plugins and configuration helpers.

The catalog describes adapters that are already part of the harness.  It does
not import or connect to an adapter; a caller opts in by creating a connector
and adding it to its :class:`~src.config.Config`.
"""

from __future__ import annotations

import re
from typing import Any

from .config import Config, ConnectorConfig

_SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,47}$")

# Keep this data static and dependency-free.  Returning copies from
# plugin_catalog prevents callers from changing the process-wide catalog.
_PLUGIN_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "name": "mcp",
        "description": "Connect to a configured MCP server; supply capabilities such as logs.",
        "capabilities": [],
    },
    {
        "name": "webhook",
        "description": "Receive incident notifications through the harness webhook intake.",
        "capabilities": ["incidents"],
    },
    {
        "name": "loki",
        "description": "Query logs and metrics from a Loki HTTP endpoint.",
        "capabilities": ["logs", "metrics"],
    },
    {
        "name": "grafana",
        "description": "Query a Grafana Loki datasource through its HTTP API.",
        "capabilities": ["logs", "metrics"],
    },
    {
        "name": "local-logs",
        "description": "Read recent lines from a local application log file.",
        "capabilities": ["logs"],
    },
)

_PLUGIN_PURPOSES = {
    "mcp": "observability",
    "webhook": "incident",
    "loki": "observability",
    "grafana": "observability",
    "local-logs": "observability",
}
_PLUGIN_OPTIONS = {
    "mcp": {"transport", "url", "command", "auth_token_env", "capabilities", "purpose"},
    "webhook": {"capabilities", "purpose"},
    "loki": {"url", "auth_token_env", "tenant_id", "capabilities", "purpose"},
    "grafana": {
        "url", "auth_token_env", "tenant_id", "datasource_uid", "capabilities", "purpose"
    },
    "local-logs": {"log_path", "capabilities", "purpose"},
}


def plugin_catalog() -> list[dict[str, Any]]:
    """Return metadata for the built-in source adapters.

    Catalog discovery has no network or filesystem side effects.
    """

    return [
        {**plugin, "capabilities": list(plugin["capabilities"])}
        for plugin in _PLUGIN_CATALOG
    ]


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name):
        raise ValueError(
            "connector name must start with a letter or underscore, contain only letters, "
            "numbers, underscore, or hyphen, and be at most 48 characters"
        )
    return name


def connector_from_plugin(plugin: str, name: str, **options: Any) -> ConnectorConfig:
    """Build a validated connector configuration for a catalog plugin.

    The returned object is configuration only.  No adapter is imported or
    contacted until the normal connector manager is started.
    """

    if plugin not in {entry["name"] for entry in _PLUGIN_CATALOG}:
        raise ValueError(f"unknown connector plugin: {plugin}")
    _validate_name(name)
    if "type" in options and options["type"] != plugin:
        raise ValueError(f"connector type must match plugin {plugin!r}")
    unsupported = set(options).difference(_PLUGIN_OPTIONS[plugin], {"type"})
    if unsupported:
        names = ", ".join(sorted(unsupported))
        raise ValueError(f"options are not supported by {plugin}: {names}")
    if plugin == "grafana" and "tenant_id" in options:
        raise ValueError("tenant_id is not supported by grafana; configure the datasource tenant")
    if plugin == "mcp":
        transport = options.get("transport", "streamable-http")
        if transport == "stdio" and "url" in options:
            raise ValueError("url is not supported for stdio MCP connectors")
        if transport == "stdio" and "auth_token_env" in options:
            raise ValueError("auth_token_env is only supported for HTTP MCP connectors")
        if transport != "stdio" and "command" in options:
            raise ValueError("command is only supported for stdio MCP connectors")
    values = {**options, "name": name, "type": plugin}
    values.setdefault("purpose", _PLUGIN_PURPOSES[plugin])
    if plugin == "mcp" and not values.get("capabilities"):
        raise ValueError("mcp connectors require at least one capability")
    return ConnectorConfig(**values)


def connect_source(config: Config, connector: ConnectorConfig) -> None:
    """Add a source configuration, rejecting duplicate or unsafe names."""

    if not isinstance(config, Config):
        raise TypeError("config must be a Config")
    if not isinstance(connector, ConnectorConfig):
        raise TypeError("connector must be a ConnectorConfig")
    _validate_name(connector.name)
    normalized_name = connector.name.replace("-", "_")
    if any(
        existing.name == connector.name
        or existing.name.replace("-", "_") == normalized_name
        for existing in config.connectors
    ):
        raise ValueError(f"connector is already configured: {connector.name}")
    config.connectors.append(connector)
