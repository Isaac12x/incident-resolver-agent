"""Local-file evidence works without Grafana, across configuration and runtime paths."""

import json
import os
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError
from textual.widgets import Button, Input, Select, Static, TabbedContent

from src.__main__ import _run_direct
from src.config import Config, ConnectorConfig, load_config, save_config
from src.connectors import ConnectorManager
from src.local_logs import MAX_OUTPUT_BYTES, MAX_READ_BYTES, LocalLogServer
from src.models import Incident, TaskState
from src.tui import ConfigurationApp


def connector(path):
    return ConnectorConfig(name="app-logs", type="local-logs", log_path=str(path))


async def read(server, **arguments):
    result = await server.call_tool("app_logs_read_logs", arguments)
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_local_evidence_discovery_health_filter_and_rotation(tmp_path):
    path = tmp_path / "app.log"
    path.write_text("INFO started\nERROR first\nINFO healthy\nERROR second\n")
    manager = ConnectorManager([connector(path)])
    await manager.start()
    assert (await manager.health())["app-logs"].connected
    (server,) = await manager.tools_for({"logs"})
    assert (await server.list_tools())[0].name == "app_logs_read_logs"
    assert (await read(server, contains="ERROR", limit=1))["lines"] == ["ERROR second"]
    assert (await read(server, contains="absent"))["lines"] == []
    path.rename(tmp_path / "app.log.1")
    path.write_text("ERROR after rotation\n")
    assert (await read(server))["lines"] == ["ERROR after rotation"]
    path.write_text("truncated file\n")
    assert (await read(server))["lines"] == ["truncated file"]
    assert (await server.list_prompts()).prompts == []
    with pytest.raises(ValueError, match="do not provide"):
        await server.get_prompt("none")
    await manager.stop()
    assert not manager.sessions


@pytest.mark.asyncio
async def test_missing_file_recovers_and_nonregular_file_is_rejected(tmp_path):
    path = tmp_path / "app.log"
    manager = ConnectorManager([connector(path)])
    await manager.start()
    (server,) = await manager.tools_for({"logs"})
    assert not (await manager.health())["app-logs"].connected
    with pytest.raises(FileNotFoundError):
        await read(server)
    path.write_bytes(b"invalid utf8 \xff\n")
    assert (await manager.health())["app-logs"].connected
    assert (await read(server))["lines"] == ["invalid utf8 \ufffd"]
    path.unlink()
    os.mkfifo(path)
    assert not (await manager.health())["app-logs"].connected
    with pytest.raises(ValueError, match="regular file"):
        await read(server)
    path.unlink()
    path.mkdir()
    assert not (await manager.health())["app-logs"].connected
    await manager.stop()


@pytest.mark.asyncio
async def test_bounds_and_arguments_cannot_override_file(tmp_path):
    path = tmp_path / "app.log"
    server = LocalLogServer(connector(path))
    path.write_bytes(b"x" * MAX_READ_BYTES + b"\nERROR recent\n")
    result = await read(server)
    assert result["lines"] == ["ERROR recent"] and result["truncated"]
    assert result["scanned_bytes"] <= MAX_READ_BYTES
    path.write_text(("x" * 1000 + "\n") * 200)
    result = await read(server, limit=200)
    assert sum(len(line.encode()) for line in result["lines"]) <= MAX_OUTPUT_BYTES
    assert result["truncated"]
    path.write_text("x" * (MAX_OUTPUT_BYTES + 1))
    assert (await read(server))["truncated"]
    path.write_text("")
    assert (await read(server))["lines"] == []
    for arguments in [
        {"path": "/etc/passwd"},
        {"limit": 201},
        {"limit": 0},
        {"contains": "x" * 4001},
    ]:
        with pytest.raises(ValidationError):
            await read(server, **arguments)
    with pytest.raises(ValueError, match="unknown read-only"):
        await server.call_tool("delete", {})


def test_config_validation_roundtrip_and_manifest(tmp_path):
    for path in [None, "", "relative.log", "/tmp/invalid\x00.log"]:
        with pytest.raises(ValidationError, match="log_path"):
            ConnectorConfig(name="logs", type="local-logs", log_path=path)
    config = Config(connectors=[connector(tmp_path / "app.log")])
    assert config.connectors[0].capabilities == ["logs"]
    path = tmp_path / "config.toml"
    save_config(config, path)
    assert load_config(path) == config
    first = ConnectorManager(config.connectors).descriptors()[0]
    second = ConnectorManager([connector(tmp_path / "other.log")]).descriptors()[0]
    assert first["endpoint_sha256"] != second["endpoint_sha256"]
    assert str(tmp_path) not in json.dumps(first)


@pytest.mark.asyncio
async def test_tui_add_test_save_reload_local_logs(tmp_path):
    path = tmp_path / "config.toml"
    log = tmp_path / "app.log"
    log.write_text("ERROR example\n")
    app = ConfigurationApp(path)
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "connections-tab"
        await pilot.pause()
        app.query_one("#add-connector", Button).press()
        await pilot.pause()
        prefix = "#connector-connector-0"
        app.query_one(f"{prefix}-name", Input).value = "app-logs"
        app.query_one(f"{prefix}-type", Select).value = "local-logs"
        app.query_one(f"{prefix}-log-path", Input).value = str(log)
        await app._test_connector("connector-0")
        assert "Connected" in str(app.query_one("#connector-status-connector-0", Static).render())
        app.action_save()
        await pilot.pause()
    saved = load_config(path)
    assert saved.connectors == [connector(log)]
    reloaded = ConfigurationApp(path)
    async with reloaded.run_test():
        assert reloaded.query_one(f"{prefix}-log-path", Input).value == str(log)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_manual_incident_run_starts_and_cleans_up_connectors(tmp_path, fail):
    path = tmp_path / "incident.json"
    path.write_text(
        Incident(
            external_id="manual",
            source="manual",
            repository="owner/repo",
            environment="production",
            summary="Investigate local application error",
        ).model_dump_json()
    )
    manager = ConnectorManager([connector(tmp_path / "app.log")])

    async def submit(incident):
        assert len(await manager.tools_for({"logs"})) == 1
        if fail:
            raise RuntimeError("submission failed")
        return Mock(state=TaskState.COMPLETED, model_dump=Mock(return_value={"ok": True}))

    application = Mock(connectors=manager, workflow=Mock(submit=AsyncMock(side_effect=submit)))
    if fail:
        with pytest.raises(RuntimeError, match="submission failed"):
            await _run_direct(application, path)
    else:
        await _run_direct(application, path)
    assert not manager.sessions


@pytest.mark.asyncio
async def test_subscription_runtime_can_call_local_log_tool(tmp_path):
    import asyncio

    from src.agent import AgentRunContext, SubscriptionCLIBackend
    from src.storage import Storage
    from src.tools import WorkspaceTools

    log = tmp_path / "app.log"
    log.write_text("ERROR local failure evidence\n")
    config = Config(runtime_root=tmp_path / "runtime", connectors=[connector(log)])
    task = Storage(config.runtime_root).create_task(
        Incident(
            external_id="local-logs",
            source="manual",
            repository="owner/repo",
            environment="production",
            summary="Local error",
        )
    )
    context = AgentRunContext(
        task=task,
        session_id=task.task_id,
        session_db=tmp_path / "session.db",
        lifecycle=Mock(),
        save_backend_session=Mock(),
        memory_writer=Mock(),
        capabilities=frozenset({"logs"}),
        connector_tools=(LocalLogServer(connector(log)),),
    )
    backend = SubscriptionCLIBackend(config)
    assert "app_logs_read_logs" in await backend._connector_help(context)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    async with backend._tool_bridge(
        context, WorkspaceTools(worktree, permissions=config.permissions)
    ):
        process = await asyncio.create_subprocess_exec(
            str(worktree / "harness-out" / "incident-session-tool"),
            "connector_call",
            json.dumps({"connector": "app-logs", "tool": "app_logs_read_logs", "arguments": {}}),
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        result = json.loads(stdout)
        assert result["ok"]
        assert "ERROR local failure evidence" in result["result"]["content"][0]["text"]
