"""Installed extension discovery during application startup."""

from __future__ import annotations

import hashlib
import hmac
import importlib
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src import __main__ as cli
from src import plugin_discovery, source_control
from src.app import Application
from src.config import Config, ModelConfig, RepositoryConfig, load_config, save_config
from src.server import create_server


@pytest.fixture(autouse=True)
def isolated_discovery_state(monkeypatch):
    monkeypatch.setattr(plugin_discovery, "_discovered", False)
    monkeypatch.setattr(plugin_discovery, "_discovering", False)
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


def test_builtin_config_validation_does_not_discover_plugins(tmp_path, monkeypatch):
    monkeypatch.setattr(
        plugin_discovery,
        "entry_points",
        lambda **kwargs: pytest.fail("built-in modes do not need plugin discovery"),
    )
    config_path = tmp_path / "config.toml"
    save_config(
        Config(repositories=[RepositoryConfig(name="org/repo", publish_mode="github")]),
        config_path,
    )

    assert load_config(config_path, create=False).repositories[0].publish_mode == "github"


def test_direct_custom_repository_config_does_not_discover_plugins(monkeypatch):
    monkeypatch.setattr(
        plugin_discovery,
        "entry_points",
        lambda **kwargs: pytest.fail("direct model construction must not discover plugins"),
    )

    source_control.register_source_control_provider(
        "entrypoint-provider", cast(source_control.ProviderFactory, lambda *_: object())
    )

    repository = RepositoryConfig(name="org/repo", publish_mode="entrypoint-provider")

    assert repository.publish_mode == "entrypoint-provider"
    assert plugin_discovery._discovered is False


def test_entry_point_enumeration_failure_logs_and_keeps_builtin_startup(
    tmp_path, monkeypatch, caplog
):
    calls = []

    def broken_enumeration(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("metadata unavailable")
        return ()

    monkeypatch.setattr(plugin_discovery, "entry_points", broken_enumeration)
    config_path = tmp_path / "config.toml"
    save_config(Config(runtime_root=tmp_path / ".agent"), config_path)

    application = Application.build(config_path, agent_backend=AsyncMock())
    plugin_discovery.discover_plugins()

    assert tuple(application.source_control) == ("github",)
    assert calls == [{"group": "incident_harness.plugins"}] * 2
    assert "failed to enumerate incident-harness plug-ins" in caplog.text


def test_partial_entry_point_enumeration_does_not_run_registrars_before_retry(monkeypatch):
    registrar_calls = []
    enumeration_calls = []

    def registrar():
        registrar_calls.append("registered")
        source_control.register_source_control_provider(
            "retry-safe-provider", lambda config, storage: object()
        )

    def broken_enumeration(**kwargs):
        enumeration_calls.append(kwargs)
        if len(enumeration_calls) == 1:
            def partial_results():
                yield EntryPoint(registrar)
                raise RuntimeError("metadata iteration failed")

            return partial_results()
        return (EntryPoint(registrar),)

    monkeypatch.setattr(plugin_discovery, "entry_points", broken_enumeration)

    plugin_discovery.discover_plugins()
    assert registrar_calls == []
    assert "retry-safe-provider" not in source_control.registered_source_control_providers()

    plugin_discovery.discover_plugins()

    assert registrar_calls == ["registered"]
    assert source_control.registered_source_control_providers() == (
        "github",
        "retry-safe-provider",
    )


def test_application_build_and_server_start_with_only_builtin_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_discovery, "entry_points", lambda **kwargs: ())
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    config_path = tmp_path / "config.toml"
    save_config(
        Config(
            runtime_root=tmp_path / ".agent",
            model=ModelConfig(mode="local", base_url="http://127.0.0.1:1234/v1"),
        ),
        config_path,
    )
    served = []

    def run_with_testclient(server, *, host, port):
        served.append((host, port))
        assert set(server.state.application.source_control) == {"github"}
        with TestClient(server) as client:
            health = client.get("/health")
            assert health.status_code == 200
            assert health.json()["worker"] == "running"
            assert client.get("/openapi.json").status_code == 200
            assert client.post("/hooks/github", content=b"{}").status_code == 401

    monkeypatch.setattr(cli.uvicorn, "run", run_with_testclient)
    cli.main(["--config", str(config_path), "serve"])

    assert len(served) == 1


def test_built_server_authenticates_real_github_webhook_with_empty_plugin_registry(
    tmp_path, monkeypatch
):
    secret = "test-github-secret"
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)
    monkeypatch.setattr(plugin_discovery, "entry_points", lambda **kwargs: ())
    config_path = tmp_path / "config.toml"
    save_config(Config(runtime_root=tmp_path / ".agent"), config_path)
    application = Application.build(config_path, agent_backend=AsyncMock())
    application.workflow.handle_source_control_event = AsyncMock(return_value=None)

    server = create_server(application, run_worker=False)
    body = b'{"zen":"Keep it logically awesome."}'
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    assert source_control.registered_source_control_providers() == ("github",)
    assert source_control._webhook_routers == []
    with TestClient(server) as client:
        response = client.post(
            "/hooks/github",
            content=body,
            headers={
                "x-hub-signature-256": f"sha256={signature}",
                "x-github-event": "ping",
                "x-github-delivery": "delivery-1",
            },
        )

    assert response.status_code == 202
    assert response.json() == {"accepted": True, "task_id": None}
    application.workflow.handle_source_control_event.assert_awaited_once_with(
        "github", "ping", {"zen": "Keep it logically awesome."}
    )


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


@pytest.mark.parametrize("path", ["/hooks/github", "/hooks/{provider}", "/hooks/{rest:path}"])
def test_external_webhook_routes_cannot_shadow_github(path):
    server = FastAPI()
    server.add_api_route("/hooks/github", lambda: {"builtin": True}, methods=["POST"])
    source_control._webhook_routers.clear()
    router = source_control.APIRouter()
    router.add_api_route(path, lambda: {"plugin": True}, methods=["POST"])
    source_control.register_webhook_router(router)

    with pytest.raises(ValueError, match="overlaps.*hooks/github"):
        source_control.mount_webhook_routers(server)


@pytest.mark.parametrize("path", ["/hooks/{id:int}", "/hooks/{name}.json"])
def test_external_webhook_routes_keep_disjoint_dynamic_paths(path):
    server = FastAPI()
    server.add_api_route("/hooks/github", lambda: {"builtin": True}, methods=["POST"])
    source_control._webhook_routers.clear()
    router = source_control.APIRouter()
    router.add_api_route(path, lambda: {"plugin": True}, methods=["POST"])
    source_control.register_webhook_router(router)

    source_control.mount_webhook_routers(server)
    with TestClient(server) as client:
        sample = "1" if ":int}" in path else "thing.json"
        response = client.post(f"/hooks/{sample}")
    assert response.json() == {"plugin": True}


def test_nested_webhook_router_prefix_is_checked():
    server = FastAPI()
    server.add_api_route("/api/hooks/github", lambda: {"builtin": True}, methods=["POST"])
    source_control._webhook_routers.clear()
    child = source_control.APIRouter()
    child.add_api_route("/hooks/{name}", lambda: {"plugin": True}, methods=["POST"])
    parent = source_control.APIRouter()
    parent.include_router(child, prefix="/api")
    source_control.register_webhook_router(parent)

    with pytest.raises(ValueError, match="overlaps.*api/hooks/github"):
        source_control.mount_webhook_routers(server)


def test_conflicting_catchall_and_dynamic_plugin_routes_are_rejected():
    server = FastAPI()
    server.add_api_route("/hooks/{name}", lambda: {"builtin": True}, methods=["POST"])
    source_control._webhook_routers.clear()
    router = source_control.APIRouter()
    router.add_api_route("/hooks/{rest:path}", lambda: {"plugin": True}, methods=["POST"])
    source_control.register_webhook_router(router)

    with pytest.raises(ValueError, match=r"overlaps.*hooks/\{name\}"):
        source_control.mount_webhook_routers(server)


@pytest.mark.parametrize(
    ("builtin_path", "plugin_path", "conflicts"),
    [
        ("/hooks/{id:int}", "/hooks/{value:float}", True),
        ("/hooks/{id:int}", "/hooks/{value:uuid}", True),
        ("/hooks/{id:int}/{repo:int}", "/hooks/{org:float}/{number:int}", True),
        ("/hooks/{id:int}/left", "/hooks/{org:float}/right", False),
    ],
)
def test_converter_route_overlap_uses_segment_types(builtin_path, plugin_path, conflicts):
    server = FastAPI()
    server.add_api_route(builtin_path, lambda: {"builtin": True}, methods=["POST"])
    source_control._webhook_routers.clear()
    router = source_control.APIRouter()
    router.add_api_route(plugin_path, lambda: {"plugin": True}, methods=["POST"])
    source_control.register_webhook_router(router)

    if conflicts:
        with pytest.raises(ValueError, match="webhook route conflicts"):
            source_control.mount_webhook_routers(server)
    else:
        source_control.mount_webhook_routers(server)


def test_get_route_does_not_claim_unregistered_head_method():
    server = FastAPI()
    server.add_api_route("/hooks/shared", lambda: {"builtin": True}, methods=["GET"])
    source_control._webhook_routers.clear()
    router = source_control.APIRouter()
    router.add_api_route("/hooks/shared", lambda: {"plugin": True}, methods=["HEAD"])
    source_control.register_webhook_router(router)

    source_control.mount_webhook_routers(server)


def test_registrar_can_validate_custom_mode_without_recursive_discovery(monkeypatch):
    calls = []

    def registrar():
        calls.append("register")
        plugin_discovery.discover_plugins()

    monkeypatch.setattr(plugin_discovery, "entry_points", lambda **kwargs: (EntryPoint(registrar),))
    plugin_discovery.discover_plugins()

    assert calls == ["register"]


def test_external_webhook_route_mount_conflicts_are_checked_before_mounting(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_discovery, "entry_points", lambda **kwargs: ())
    config_path = tmp_path / "config.toml"
    save_config(Config(runtime_root=tmp_path / ".agent"), config_path)
    application = Application.build(config_path, agent_backend=AsyncMock())
    first = source_control.APIRouter()
    first.add_api_route("/hooks/shared", lambda: {"first": True}, methods=["POST"])
    second = source_control.APIRouter()
    second.add_api_route("/hooks/shared", lambda: {"second": True}, methods=["POST"])
    source_control.register_webhook_router(first)
    source_control.register_webhook_router(second)

    with pytest.raises(ValueError, match="overlaps.*hooks/shared"):
        create_server(application, run_worker=False)


def test_create_server_preflights_plugins_against_native_github_route(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_discovery, "entry_points", lambda **kwargs: ())
    config_path = tmp_path / "config.toml"
    save_config(Config(runtime_root=tmp_path / ".agent"), config_path)
    application = Application.build(config_path, agent_backend=AsyncMock())
    router = source_control.APIRouter()
    router.add_api_route("/hooks/github", lambda: {"plugin": True}, methods=["POST"])
    source_control.register_webhook_router(router)

    with pytest.raises(ValueError, match="overlaps.*hooks/github"):
        create_server(application, run_worker=False)


def test_router_routes_added_after_registration_are_checked_before_server_mount(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(plugin_discovery, "entry_points", lambda **kwargs: ())
    config_path = tmp_path / "config.toml"
    save_config(Config(runtime_root=tmp_path / ".agent"), config_path)
    application = Application.build(config_path, agent_backend=AsyncMock())
    router = source_control.APIRouter()
    source_control.register_webhook_router(router)
    router.add_api_route("/hooks/github", lambda: {"plugin": True}, methods=["POST"])

    with pytest.raises(ValueError, match="overlaps.*hooks/github"):
        create_server(application, run_worker=False)


def test_registered_router_mutation_after_server_build_cannot_shadow_native_route(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(plugin_discovery, "entry_points", lambda **kwargs: ())
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret")
    config_path = tmp_path / "config.toml"
    save_config(Config(runtime_root=tmp_path / ".agent"), config_path)
    application = Application.build(config_path, agent_backend=AsyncMock())
    router = source_control.APIRouter()
    source_control.register_webhook_router(router)
    server = create_server(application, run_worker=False)

    # The running server uses the routes that passed its construction-time check.
    router.add_api_route("/hooks/github", lambda: {"plugin": True}, methods=["POST"])
    router.add_api_route("/hooks/late", lambda: {"plugin": True}, methods=["POST"])

    with TestClient(server) as client:
        native = client.post("/hooks/github", content=b"{}")
        late = client.post("/hooks/late", content=b"{}")

    assert native.status_code == 401
    assert native.json() == {"detail": "invalid GitHub webhook signature"}
    assert late.status_code == 404


def test_child_router_mutation_after_server_build_does_not_add_unchecked_route(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(plugin_discovery, "entry_points", lambda **kwargs: ())
    config_path = tmp_path / "config.toml"
    save_config(Config(runtime_root=tmp_path / ".agent"), config_path)
    application = Application.build(config_path, agent_backend=AsyncMock())
    child = source_control.APIRouter()
    parent = source_control.APIRouter()
    parent.include_router(child, prefix="/provider")
    source_control.register_webhook_router(parent)
    server = create_server(application, run_worker=False)

    child.add_api_route("/hooks/late", lambda: {"plugin": True}, methods=["POST"])

    with TestClient(server) as client:
        response = client.post("/provider/hooks/late", content=b"{}")

    assert response.status_code == 404


def test_nested_router_snapshot_preserves_route_options_and_dependencies(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_discovery, "entry_points", lambda **kwargs: ())
    config_path = tmp_path / "config.toml"
    save_config(Config(runtime_root=tmp_path / ".agent"), config_path)
    application = Application.build(config_path, agent_backend=AsyncMock())
    dependency_calls = []

    def authenticate():
        dependency_calls.append("checked")

    child = source_control.APIRouter()
    child.add_api_route(
        "/event",
        lambda: {"accepted": True},
        methods=["POST"],
        status_code=202,
        dependencies=[Depends(authenticate)],
    )
    parent = source_control.APIRouter()
    parent.include_router(child, prefix="/provider")
    source_control.register_webhook_router(parent)
    server = create_server(application, run_worker=False)

    with TestClient(server) as client:
        response = client.post("/provider/event")

    assert response.status_code == 202
    assert response.json() == {"accepted": True}
    assert dependency_calls == ["checked"]


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
    config_path.write_text(
        'runtime_root = "' + str(tmp_path / ".agent") + '"\n'
        '[[repositories]]\nname = "org/repo"\npublish_mode = "entrypoint-provider"\n',
        encoding="utf-8",
    )
    assert load_config(config_path, create=False).repositories[0].publish_mode == (
        "entrypoint-provider"
    )

    application = Application.build(config_path, agent_backend=AsyncMock())

    assert application.source_control["entrypoint-provider"].name == "discovered"
    assert "github" in application.source_control
