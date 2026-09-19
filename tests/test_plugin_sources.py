"""Named CLI sources remain independently usable by the running harness."""

import json

import pytest

from src.__main__ import main
from src.config import load_config
from src.connectors import ConnectorManager


@pytest.mark.asyncio
async def test_cli_sources_discover_and_read_independent_logs(tmp_path):
    config_path = tmp_path / "config.toml"
    for name in ("api", "worker"):
        path = tmp_path / f"{name}.log"
        path.write_text(f"ERROR {name} failure\n")
        main([
            "--config", str(config_path), "connect", "local-logs",
            "--name", name, "--log-path", str(path),
        ])

    manager = ConnectorManager(load_config(config_path, create=False).connectors)
    assert not manager.sessions
    await manager.start()
    try:
        servers = await manager.tools_for({"logs"})
        assert {server.name for server in servers} == {"api", "worker"}
        for server in servers:
            tool, = await server.list_tools()
            result = await server.call_tool(tool.name, {})
            assert json.loads(result.content[0].text)["lines"] == [
                f"ERROR {server.name} failure"
            ]
        assert await manager.tools_for({"unconfigured-capability"}) == []
    finally:
        await manager.stop()
    assert not manager.sessions
