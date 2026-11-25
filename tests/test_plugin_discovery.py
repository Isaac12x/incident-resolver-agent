"""Installed extension discovery during application startup."""

from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from src import plugin_discovery, source_control
from src.app import Application
from src.config import Config, RepositoryConfig, save_config
from src.server import create_server


@pytest.fixture(autouse=True)
def isolated_discovery_state(monkeypatch):
    monkeypatch.setattr(plugin_discovery, "_discovered", False)
    monkeypatch.setattr(plugin_discovery, "_failure", None)
    monkeypatch.setattr(
        source_control, "_provider_factories", source_control._provider_factories.copy()
    )
    monkeypatch.setattr(source_control, "_webhook_routers", source_control._webhook_routers.copy())


class EntryPoint:
    name = "fixture"

    def __init__(self, registrar):
        self.registrar = registrar

    def load(self):
        return self.registrar


def test_discovery_succeeds_with_no_plugins_and_is_idempotent(monkeypatch):
    calls = []

    def no_plugins(**kwargs):
        calls.append(kwargs)
        return ()

    monkeypatch.setattr(plugin_discovery, "entry_points", no_plugins)

    plugin_discovery.discover_plugins()
    plugin_discovery.discover_plugins()

    assert calls == [{"group": "incident_harness.plugins"}]


def test_application_build_and_server_start_with_only_builtin_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_discovery, "entry_points", lambda **kwargs: ())
    config_path = tmp_path / "config.toml"
    save_config(Config(runtime_root=tmp_path / ".agent"), config_path)

    application = Application.build(config_path, agent_backend=AsyncMock())
    server = create_server(application, run_worker=False)

    assert tuple(application.source_control) == ("github",)
    with TestClient(server) as client:
        assert client.get("/openapi.json").status_code == 200


def test_discovery_registers_plugins_once_across_concurrent_calls(monkeypatch):
    entered = Event()
    release = Event()
    calls = []

    def registrar():
        entered.set()
        assert release.wait(timeout=5)
        calls.append("registered")

    monkeypatch.setattr(plugin_discovery, "entry_points", lambda **kwargs: (EntryPoint(registrar),))
    with ThreadPoolExecutor(max_workers=4) as executor:
        first = executor.submit(plugin_discovery.discover_plugins)
        assert entered.wait(timeout=5)
        others = [executor.submit(plugin_discovery.discover_plugins) for _ in range(3)]
        release.set()
        first.result(timeout=5)
        for other in others:
            other.result(timeout=5)

    assert calls == ["registered"]


def test_discovery_failure_stops_startup_and_is_not_retried(monkeypatch):
    calls = []

    def registrar():
        calls.append("called")
        raise ValueError("registrar failed")

    monkeypatch.setattr(plugin_discovery, "entry_points", lambda **kwargs: (EntryPoint(registrar),))

    with pytest.raises(ValueError, match="registrar failed"):
        plugin_discovery.discover_plugins()
    with pytest.raises(RuntimeError, match="previously failed"):
        plugin_discovery.discover_plugins()

    assert calls == ["called"]


def test_discovery_rejects_non_callable_registrar(monkeypatch):
    monkeypatch.setattr(plugin_discovery, "entry_points", lambda **kwargs: (EntryPoint(None),))

    with pytest.raises(TypeError, match="not callable: fixture"):
        plugin_discovery.discover_plugins()


def test_application_build_discovers_installed_entry_point_plugins(tmp_path, monkeypatch):
    plugin_name = "fixture_installed_plugin"
    (tmp_path / f"{plugin_name}.py").write_text(
        "from types import SimpleNamespace\n"
        "from src.source_control import register_source_control_provider\n"
        "def register():\n"
        "    register_source_control_provider(\n"
        "        'entrypoint-provider',\n"
        "        lambda config, storage: SimpleNamespace(name='discovered'),\n"
        "    )\n",
        encoding="utf-8",
    )
    metadata = tmp_path / "fixture_installed_plugin-1.0.dist-info"
    metadata.mkdir()
    (metadata / "entry_points.txt").write_text(
        "[incident_harness.plugins]\nfixture = fixture_installed_plugin:register\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    config_path = tmp_path / "config.toml"
    save_config(
        Config(
            runtime_root=tmp_path / ".agent",
            repositories=[RepositoryConfig(name="org/repo", publish_mode="entrypoint-provider")],
        ),
        config_path,
    )

    application = Application.build(config_path, agent_backend=AsyncMock())

    assert application.source_control["entrypoint-provider"].name == "discovered"
    assert "github" in application.source_control
