"""Safe, actionable messages for connector connection failures."""

from __future__ import annotations

import re

import httpx


class ConnectorHTTPError(RuntimeError):
    """An upstream HTTP response failure, stored without its URL or response body."""

    def __init__(self, status_code: int, connector_name: str | None = None) -> None:
        self.status_code = status_code
        prefix = f"{connector_name}: " if connector_name else ""
        super().__init__(f"{prefix}HTTP {status_code}")


def connector_http_message(status: int) -> str:
    """Return guidance for an HTTP status without exposing the request details."""
    if status == 401:
        return "Authentication failed (HTTP 401). Check the token and its environment variable."
    if status == 403:
        return "Access denied (HTTP 403). Check the token's permissions and tenant."
    if status == 404:
        return (
            "Endpoint not found (HTTP 404). Check the URL and, for Grafana, the Loki datasource "
            "UID."
        )
    if status == 429:
        return "The connector is rate limited (HTTP 429). Retry after a short wait."
    if status >= 500:
        return f"The connector server returned HTTP {status}. Check its health and try again."
    return f"The connector returned HTTP {status}. Check its endpoint settings."


def connector_error_message(error: Exception) -> str:
    """Describe a failed connection without exposing exception internals or secrets."""
    status = getattr(error, "status_code", None)
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
    if status is not None:
        return connector_http_message(status)

    message = str(error)
    missing_env = re.search(
        r"missing environment variable\s+([A-Za-z_][A-Za-z0-9_]*)"
        r"|environment variable\s+([A-Za-z_][A-Za-z0-9_]*)\s+"
        r"(?:is not configured|is not set|is missing)",
        message,
        re.I,
    )
    if missing_env:
        variable = missing_env.group(1) or missing_env.group(2)
        return (
            f"Environment variable {variable} is not set. "
            "Add its credential and retry."
        )
    if isinstance(error, (TimeoutError, httpx.TimeoutException)) or "timed out" in message.lower():
        return "Connection timed out. Check that the endpoint is reachable and try again."
    if (
        isinstance(error, (OSError, httpx.HTTPError))
        or "connection failed" in message.lower()
    ):
        return "Could not reach the connector. Check the endpoint and network access."
    if isinstance(error, ValueError) and message in {
        "Grafana database is not healthy",
        "Grafana Loki datasource is not healthy",
    }:
        return message + ". Check Grafana and its Loki datasource configuration."
    return "Connection check failed. Review the endpoint, credentials, and connector logs."
