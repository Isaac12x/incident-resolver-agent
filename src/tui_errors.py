"""Plain, actionable error messages for the configuration editor."""

from __future__ import annotations

import errno
import re

from pydantic import ValidationError


def format_tui_error(error: Exception) -> str:
    """Translate validation failures into concise text suitable for TUI status widgets."""
    if isinstance(error, ValidationError):
        messages = []
        for item in error.errors(include_input=False, include_url=False):
            location = ".".join(map(str, item["loc"]))
            message = item["msg"]
            if message.startswith("Value error, "):
                message = message.removeprefix("Value error, ")
            if "Grafana connectors require a Loki datasource_uid" in message:
                message = "Grafana needs a Loki datasource UID. Enter the UID from Grafana."
            elif "HTTP connectors require url" in message:
                message = "HTTP connections need a URL. Enter the server address."
            elif "Loki/Grafana connectors require an HTTP(S) URL" in message:
                message = "Loki and Grafana connections need an HTTP(S) server URL."
            elif "stdio connectors require command" in message:
                message = "This MCP connection needs a command to start its server."
            elif "local-logs connectors require an absolute log_path" in message:
                message = "Local logs need an absolute path to the log file."
            elif "local-logs connectors require" in message:
                message = "Local log connections need the required log settings."
            elif item["type"] == "literal_error":
                choices = item.get("ctx", {}).get("expected", "the listed options")
                field = location.rsplit(".", 1)[-1].replace("_", " ") or "value"
                message = f"Choose a valid {field} ({choices})."
            messages.append(f"{location}: {message}" if location else message)
        return "; ".join(messages)

    if isinstance(error, OSError):
        if error.errno == errno.ENOSPC or "disk full" in str(error).lower():
            return "Not enough disk space to write the config. Free space and retry."
        return "Could not write the config file. Check the path and file permissions."
    message = str(error).strip()
    if message:
        return format_tui_message(message)
    return "The operation failed. Check the connection settings and try again."


def format_tui_message(message: str) -> str:
    """Redact sensitive internals from legacy or injected adapter result strings."""
    if "[type=" in message or "validation error" in message.lower():
        return "Connection settings are invalid. Review the connection fields and try again."
    message = re.sub(r"https?://[^\s\])}]+", "[endpoint]", message)
    message = re.sub(r"\s*For further information visit: \[endpoint\]", "", message)
    return message.strip()
