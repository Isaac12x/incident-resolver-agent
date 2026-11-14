from __future__ import annotations

import httpx
import pytest
from textual.widgets import Button, Input, Select, Static, TabbedContent

from src.config import ConnectorConfig
from src.connector_errors import ConnectorHTTPError
from src.connectors import ConnectorManager
from src.tui import ConfigurationApp
from src.tui_errors import format_tui_error


def test_validation_error_uses_actionable_plain_message() -> None:
    with pytest.raises(ValueError) as caught:
        ConnectorConfig(
            name="grafana",
            type="grafana",
            url="https://grafana.example",
        )

    message = format_tui_error(caught.value)
    assert "Grafana needs a Loki datasource UID" in message
    assert "[type=" not in message
    assert "pydantic" not in message.lower()


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"type": "mcp", "transport": "streamable-http"}, "need a URL"),
        ({"type": "mcp", "transport": "stdio"}, "needs a command"),
        ({"type": "local-logs", "log_path": "relative.log"}, "absolute path"),
        ({"type": "loki", "url": "not a URL"}, "HTTP(S) server URL"),
        ({"type": "grafana", "url": "https://grafana.example"}, "datasource UID"),
        ({"type": "invalid"}, "valid type"),
    ],
)
def test_type_specific_validation_errors_are_plain_and_actionable(values, expected) -> None:
    with pytest.raises(ValueError) as caught:
        ConnectorConfig(name="test", **values)

    message = format_tui_error(caught.value)
    assert expected in message
    assert "[type=" not in message
    assert "input_value" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ConnectorHTTPError(401), "Check the token"),
        (
            httpx.HTTPStatusError(
                "https://user:secret@host/path",
                request=httpx.Request("GET", "https://user:secret@host/path"),
                response=httpx.Response(401),
            ),
            "Check the token",
        ),
        (ConnectorHTTPError(403), "permissions"),
        (ConnectorHTTPError(429), "rate limited"),
        (ConnectorHTTPError(503), "server returned HTTP 503"),
        (ConnectorHTTPError(418), "Check its endpoint settings"),
        (TimeoutError(), "timed out"),
        (ValueError("missing environment variable LOKI_TOKEN"), "LOKI_TOKEN"),
        (httpx.ConnectError("https://user:secret@host/path?token=hidden"), "Could not reach"),
        (RuntimeError("https://user:secret@host/internal traceback detail"), "Review the endpoint"),
        (ValueError("Grafana database is not healthy"), "Check Grafana"),
    ],
)
async def test_manager_returns_safe_actionable_connection_errors(error, expected) -> None:
    async def fail(_config):
        raise error

    config = ConnectorConfig(name="logs", type="mcp", url="https://example.invalid")
    result = await ConnectorManager([config], factories={"logs": fail}).test_connection("logs")

    assert not result.connected
    assert expected in result.message
    assert "secret" not in result.message
    assert "https://" not in result.message


def test_tui_error_formatter_preserves_safe_numeric_errors_and_redacts_urls() -> None:
    assert format_tui_error(ValueError("max_tasks must be a number")) == (
        "max_tasks must be a number"
    )
    assert "[endpoint]" in format_tui_error(
        ValueError("could not reach https://user:secret@example.invalid/path")
    )
    assert "secret" not in format_tui_error(
        ValueError("could not reach https://user:secret@example.invalid/path")
    )
    assert "Connection settings are invalid" in format_tui_error(
        ValueError("ValidationError: [type=string_type]")
    )
    assert "file permissions" in format_tui_error(OSError("permission denied"))
    assert "try again" in format_tui_error(RuntimeError())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "Check the token"), (403, "permissions"), (404, "Check the URL")],
)
async def test_webhook_http_errors_include_guidance_without_endpoint(
    status, expected, monkeypatch
) -> None:
    config = ConnectorConfig(name="receiver", type="webhook", url="https://secret.example/hook")

    async def probe(_config):
        return status

    monkeypatch.setattr(ConnectorManager, "_probe_webhook_url", staticmethod(probe))
    result = await ConnectorManager([config]).test_connection("receiver")

    assert not result.connected
    assert expected in result.message
    assert "https://" not in result.message


@pytest.mark.asyncio
async def test_test_connection_reports_action_for_invalid_connector(tmp_path) -> None:
    app = ConfigurationApp(tmp_path / "config.toml")

    async with app.run_test(size=(80, 30)) as pilot:
        app.query_one("#content", TabbedContent).active = "connections-tab"
        await pilot.pause()
        app.query_one("#add-connector", Button).press()
        await pilot.pause()
        app.query_one("#connector-connector-0-name", Input).value = "grafana"
        app.query_one("#connector-connector-0-type", Select).value = "grafana"
        app.query_one("#connector-connector-0-url", Input).value = "https://grafana.example"
        await pilot.pause()

        await app._test_connector("connector-0")  # noqa: SLF001

        status = app.query_one("#connector-status-connector-0", Static)
        assert "Enter the UID from Grafana" in status.render().plain
        assert status.has_class("error")
        assert not status.has_class("pending")


@pytest.mark.asyncio
async def test_save_reports_validation_errors_as_plain_text(tmp_path) -> None:
    app = ConfigurationApp(tmp_path / "config.toml")

    async with app.run_test(size=(80, 30)) as pilot:
        app.query_one("#content", TabbedContent).active = "connections-tab"
        await pilot.pause()
        app.query_one("#add-connector", Button).press()
        await pilot.pause()
        app.query_one("#connector-connector-0-name", Input).value = "grafana"
        app.query_one("#connector-connector-0-type", Select).value = "grafana"
        app.query_one("#connector-connector-0-url", Input).value = "https://grafana.example"
        await pilot.pause()

        app.query_one("#save", Button).press()
        await pilot.pause()

        status = app.query_one("#status", Static)
        assert "Grafana needs a Loki datasource UID" in status.render().plain
        assert "[type=" not in status.render().plain
