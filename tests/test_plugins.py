import pytest

from src.config import Config
from src.plugins import connect_source, connector_from_plugin, plugin_catalog


def test_plugin_catalog_lists_built_in_adapters_without_shared_mutation() -> None:
    catalog = plugin_catalog()
    assert {entry["name"] for entry in catalog} == {
        "mcp", "webhook", "loki", "grafana", "local-logs"
    }
    catalog[0]["capabilities"].append("changed")
    assert "changed" not in plugin_catalog()[0]["capabilities"]


def test_connector_from_plugin_builds_native_and_mcp_configs(tmp_path) -> None:
    local = connector_from_plugin("local-logs", "app-logs", log_path=str(tmp_path / "app.log"))
    mcp = connector_from_plugin(
        "mcp", "incident-tools", transport="stdio", command=["incident-mcp"], capabilities=["logs"]
    )
    assert (local.type, local.capabilities) == ("local-logs", ["logs"])
    assert (mcp.type, mcp.transport, mcp.command) == ("mcp", "stdio", ["incident-mcp"])
    assert (local.purpose, mcp.purpose) == ("observability", "observability")


def test_connector_from_plugin_rejects_unknown_plugin_and_unsafe_name() -> None:
    with pytest.raises(ValueError, match="unknown connector plugin"):
        connector_from_plugin("unknown", "source")
    with pytest.raises(ValueError, match="connector name"):
        connector_from_plugin("webhook", "bad name")
    with pytest.raises(ValueError, match="at most 48"):
        connector_from_plugin("webhook", "a" * 49)
    with pytest.raises(ValueError, match="start with"):
        connector_from_plugin("webhook", "1source")
    with pytest.raises(ValueError, match="require at least one capability"):
        connector_from_plugin("mcp", "tools", transport="stdio", command=["mcp"])


def test_connector_from_plugin_rejects_irrelevant_options() -> None:
    with pytest.raises(ValueError, match="not supported"):
        connector_from_plugin("local-logs", "app", log_path="/tmp/app.log", url="http://unused")


def test_connector_from_plugin_rejects_mcp_transport_mismatches() -> None:
    with pytest.raises(ValueError, match="auth_token_env is only supported"):
        connector_from_plugin(
            "mcp", "tools", transport="stdio", command=["mcp"],
            auth_token_env="TOKEN", capabilities=["logs"],
        )
    with pytest.raises(ValueError, match="url is not supported"):
        connector_from_plugin(
            "mcp", "tools", transport="stdio", url="http://unused", capabilities=["logs"]
        )
    with pytest.raises(ValueError, match="command is only supported"):
        connector_from_plugin(
            "mcp", "tools", transport="sse", command=["mcp"], capabilities=["logs"]
        )


def test_connector_from_plugin_rejects_grafana_tenant_and_native_missing_fields() -> None:
    with pytest.raises(ValueError, match="tenant_id is not supported"):
        connector_from_plugin(
            "grafana", "dash", url="https://grafana", datasource_uid="logs", tenant_id="x"
        )
    with pytest.raises(ValueError, match=r"HTTP\(S\) URL"):
        connector_from_plugin("loki", "logs")
    with pytest.raises(ValueError, match="datasource_uid"):
        connector_from_plugin("grafana", "dash", url="https://grafana")
    with pytest.raises(ValueError, match="absolute log_path"):
        connector_from_plugin("local-logs", "app", log_path="relative.log")


def test_connect_source_rejects_duplicate_and_preserves_existing_source() -> None:
    config = Config()
    first = connector_from_plugin("webhook", "alerts")
    second = connector_from_plugin("webhook", "alerts")
    connect_source(config, first)
    with pytest.raises(ValueError, match="already configured"):
        connect_source(config, second)
    assert config.connectors == [first]


def test_connect_source_rejects_native_tool_name_collision() -> None:
    config = Config()
    connect_source(config, connector_from_plugin("webhook", "app-logs"))
    with pytest.raises(ValueError, match="already configured"):
        connect_source(config, connector_from_plugin("webhook", "app_logs"))
