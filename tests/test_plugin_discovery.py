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


def test_entry_point_enumeration_failure_logs_and_keeps_builtin_startup(
    tmp_path, monkeypatch, caplog
):
    calls = []

    def broken_enumeration(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("metadata unavailable")

    monkeypatch.setattr(plugin_discovery, "entry_points", broken_enumeration)
    config_path = tmp_path / "config.toml"
    save_config(Config(runtime_root=tmp_path / ".agent"), config_path)

    application = Application.build(config_path, agent_backend=AsyncMock())
    plugin_discovery.discover_plugins()

    assert tuple(application.source_control) == ("github",)
    assert calls == [{"group": "incident_harness.plugins"}]
    assert "failed to enumerate incident-harness plug-ins" in caplog.text


def test_application_build_and_server_start_with_only_builtin_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_discovery, "entry_points", lambda **kwargs: ())
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    config_path = tmp_path / "config.toml"
    save_config(Config(runtime_root=tmp_path / ".agent"), config_path)

    application = Application.build(config_path, agent_backend=AsyncMock())
    server = create_server(application, run_worker=False)

    assert tuple(application.source_control) == ("github",)
    with TestClient(server) as client:
        assert client.get("/openapi.json").status_code == 200
        assert client.post("/hooks/github", content=b"{}").status_code == 401


def test_create_server_runs_discovery_for_existing_application(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_discovery, "entry_points", lambda **kwargs: ())
    config_path = tmp_path / "config.toml"
    save_config(Config(runtime_root=tmp_path / ".agent"), config_path)
    application = Application.build(config_path, agent_backend=AsyncMock())
    router = source_control.APIRouter()
    router.add_api_route("/hooks/discovered", lambda: {"ok": True}, methods=["GET"])

    def register():
        source_control.register_webhook_router(router)

    monkeypatch.setattr(plugin_discovery, "_discovered", False)
    monkeypatch.setattr(plugin_discovery, "entry_points", lambda **kwargs: (EntryPoint(register),))
    server = create_server(application, run_worker=False)

    with TestClient(server) as client:
        assert client.get("/hooks/discovered").json() == {"ok": True}


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


def test_failed_registrar_rolls_back_and_later_registrar_runs(monkeypatch, caplog):
    calls = []

    def registrar():
        calls.append("called")
        source_control.register_source_control_provider("partial-provider", lambda *_: None)
        raise ValueError("registrar failed")

    def later():
        calls.append("later")
        source_control.register_source_control_provider("later-provider", lambda *_: None)

    monkeypatch.setattr(plugin_discovery, "entry_points", lambda **kwargs: (
        EntryPoint(registrar), EntryPoint(later)
    ))

    plugin_discovery.discover_plugins()
    plugin_discovery.discover_plugins()

    assert calls == ["called", "later"]
    assert "partial-provider" not in source_control._provider_factories
    assert "later-provider" in source_control._provider_factories
    assert "skipping" in caplog.text


def test_failed_plugin_import_rolls_back_registrations(monkeypatch):
    class ImportFailure:
        name = "broken-import"

        def load(self):
            source_control.register_source_control_provider("import-partial", lambda *_: None)
            raise ImportError("broken extension import")

    monkeypatch.setattr(plugin_discovery, "entry_points", lambda **kwargs: (ImportFailure(),))

    plugin_discovery.discover_plugins()

    assert "import-partial" not in source_control._provider_factories


def test_discovery_rejects_non_callable_registrar(monkeypatch):
    monkeypatch.setattr(plugin_discovery, "entry_points", lambda **kwargs: (EntryPoint(None),))
    plugin_discovery.discover_plugins()
    assert plugin_discovery._discovered


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
