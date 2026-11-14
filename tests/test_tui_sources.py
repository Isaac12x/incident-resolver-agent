from __future__ import annotations

import pytest
from textual.widgets import Button, Input, Select, Static, TabbedContent

from src.config import Config, ConnectorConfig, load_config, save_config
from src.connectors import ConnectorTestResult
from src.tui import ConfigurationApp


@pytest.mark.asyncio
async def test_source_type_setup_guidance_and_webhook_intake(tmp_path) -> None:
    path = tmp_path / "config.toml"
    config = Config()
    config.trigger.hook_path = "/custom/incidents"
    config.server.webhook_secret_env = "CUSTOM_INTAKE_SECRET"
    save_config(config, path)
    app = ConfigurationApp(path)

    async with app.run_test(size=(64, 28)) as pilot:
        tabs = app.query_one("#content", TabbedContent)
        tabs.active = "connections-tab"
        await pilot.pause()
        app.query_one("#add-connector", Button).press()
        await pilot.pause()
        prefix = "connector-connector-0"

        kind = app.query_one(f"#{prefix}-type", Select)
        kind.value = "webhook"
        await pilot.pause()
        help_text = app.query_one(f"#{prefix}-type-help", Static).render().plain
        assert "public URL" in help_text
        assert "sends no incident" in help_text
        assert not app.query_one("#test-connector-0", Button).disabled
        assert app.query_one(f"#{prefix}-url-field").display
        assert app.query_one(f"#{prefix}-auth-field").display

        app.query_one("#trigger-hook-path", Input).value = "/edited/hook"
        app.query_one("#server-webhook-secret-env", Input).value = "EDITED_SECRET"
        app.query_one(f"#{prefix}-name", Input).value = "edited-source"
        await pilot.pause()
        help_text = app.query_one(f"#{prefix}-type-help", Static).render().plain
        assert "sends no incident" in help_text

        kind.value = "local-logs"
        await pilot.pause()
        assert not app.query_one("#test-connector-0", Button).disabled
        assert app.query_one(f"#{prefix}-log-path-field").display
        assert "absolute" in app.query_one(f"#{prefix}-type-help", Static).render().plain
        assert app.query_one(f"#{prefix}-name", Input).region.width > 0


@pytest.mark.asyncio
async def test_webhook_hidden_legacy_endpoint_survives_save(tmp_path) -> None:
    path = tmp_path / "config.toml"
    config = Config(
        connectors=[ConnectorConfig(
            name="alerts",
            purpose="incident",
            type="webhook",
            url="https://legacy.invalid/unused",
            auth_token_env="LEGACY_TOKEN",
        )]
    )
    save_config(config, path)
    app = ConfigurationApp(path)

    async with app.run_test(size=(100, 36)) as pilot:
        app.query_one("#content", TabbedContent).active = "connections-tab"
        await pilot.pause()
        assert app.query_one("#connector-connector-0-url-field").display
        assert app.query_one("#connector-connector-0-auth-field").display
        assert app.query_one("#connector-connector-0-url", Input).value == "https://legacy.invalid/unused"
        app.query_one("#save", Button).press()
        await pilot.pause()

    saved = load_config(path)
    assert saved.connectors[0].url == "https://legacy.invalid/unused"
    assert saved.connectors[0].auth_token_env == "LEGACY_TOKEN"


@pytest.mark.asyncio
async def test_source_connection_feedback_has_success_and_error_states(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "config.toml"
    app = ConfigurationApp(path)

    async with app.run_test(size=(80, 30)) as pilot:
        app.query_one("#content", TabbedContent).active = "connections-tab"
        await pilot.pause()
        app.query_one("#add-connector", Button).press()
        await pilot.pause()
        app.query_one("#connector-connector-0-type", Select).value = "local-logs"
        await pilot.pause()
        app.query_one("#connector-connector-0-log-path", Input).value = "/tmp/app.log"

        status = app.query_one("#connector-status-connector-0", Static)

        async def connected(self, name):
            assert status.has_class("pending")
            return ConnectorTestResult(name, True, "connected")

        async def failed(self, name):
            return ConnectorTestResult(name, False, "not reachable")

        monkeypatch.setattr("src.tui.ConnectorManager.test_connection", connected)
        await app._test_connector("connector-0")  # noqa: SLF001
        assert "Connected" in status.render().plain
        assert status.has_class("success")
        assert not status.has_class("pending")

        monkeypatch.setattr("src.tui.ConnectorManager.test_connection", failed)
        await app._test_connector("connector-0")  # noqa: SLF001
        assert "not reachable" in status.render().plain
        assert status.has_class("error")
        assert not status.has_class("success")

        app.query_one("#connector-connector-0-type", Select).value = "mcp"
        await pilot.pause()
        await app._test_connector("connector-0")  # noqa: SLF001
        assert "Invalid connection" in status.render().plain
        assert status.has_class("error")
        assert not status.has_class("success")
        assert not status.has_class("pending")
