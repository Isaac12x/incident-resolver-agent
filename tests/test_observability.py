"""Regression coverage for live readiness and the evidence-query path."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.app import Application
from src.config import Config, ConnectorConfig, RepositoryConfig, load_config, save_config
from src.connectors import ConnectorManager
from src.github import GitHubCLIAdapter
from src.observability import ObservabilityServer
from src.server import create_server
from src.storage import Storage


@pytest.fixture
def loki():
    return ConnectorConfig(name="loki", type="loki", url="http://logs.test", tenant_id="tenant-a")


@pytest.fixture
def grafana():
    return ConnectorConfig(
        name="grafana",
        type="grafana",
        url="http://grafana.test:3200",
        datasource_uid="loki-sally",
        auth_token_env="GRAFANA_TEST_TOKEN",
    )


def mock_http(monkeypatch, handler):
    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_type(transport=transport, **kw))


@pytest.mark.asyncio
async def test_tenant_logs_available_to_agent_and_health(monkeypatch, loki):
    requests = []

    def response(request):
        requests.append(request)
        assert request.headers["X-Scope-OrgID"] == "tenant-a"
        if request.url.path.endswith("labels"):
            return httpx.Response(200, json={"status": "success", "data": ["app"]})
        assert request.url.params["start"] == "2026-08-31T23:26:20Z"
        assert request.url.params["end"] == "2026-08-31T23:28:20Z"
        assert request.url.params["limit"] == "100"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "streams",
                    "result": [
                        {
                            "stream": {"app": "billing"},
                            "values": [["123", "original incident exception"]],
                        }
                    ],
                    "stats": {"large": "noise"},
                },
            },
        )

    mock_http(monkeypatch, response)
    manager = ConnectorManager([loki])
    await manager.start()
    (server,) = await manager.tools_for({"logs"})
    assert (await manager.health())["loki"].connected
    tools = await server.list_tools()
    assert [t.name for t in tools] == ["loki_query_range", "loki_labels"]
    result = await server.call_tool(
        "loki_query_range",
        {
            "query": '{app="billing"}',
            "start": "2026-08-31T23:26:20Z",
            "end": "2026-08-31T23:28:20Z",
        },
    )
    assert "original incident exception" in result.content[0].text
    assert "stats" not in result.content[0].text
    assert json.loads((await server.call_tool("loki_labels", {})).content[0].text)["data"] == [
        "app"
    ]
    assert (await server.list_prompts()).prompts == []
    with pytest.raises(ValueError, match="do not provide"):
        await server.get_prompt("missing")
    await manager.stop()
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_grafana_checks_authenticated_datasource_and_proxy(monkeypatch, grafana):
    paths = []
    monkeypatch.setenv("GRAFANA_TEST_TOKEN", "private-token")

    def response(request):
        paths.append(request.url.path)
        assert request.headers["authorization"] == "Bearer private-token"
        assert "X-Scope-OrgID" not in request.headers  # Grafana owns datasource tenant headers.
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"database": "ok"})
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "OK"})
        return httpx.Response(200, json={"status": "success", "data": []})

    mock_http(monkeypatch, response)
    server = ObservabilityServer(grafana)
    await server.check_health()
    await server.call_tool("grafana_labels", {})
    assert paths[:3] == [
        "/api/health",
        "/api/datasources/uid/loki-sally/health",
        "/api/datasources/proxy/uid/loki-sally/loki/api/v1/labels",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [401, 503, 302, "timeout", "malformed", "oversize"])
async def test_failures_are_bounded_and_do_not_echo_secrets(monkeypatch, loki, failure):
    def response(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("private-token upstream body", request=request)
        if failure == "malformed":
            return httpx.Response(200, json={"status": "error", "error": "private-token"})
        if failure == "oversize":
            return httpx.Response(200, content=b"a" * 1_000_001)
        return httpx.Response(failure, text="private-token upstream body")

    mock_http(monkeypatch, response)
    result = await ConnectorManager([loki]).test_connection("loki")
    assert not result.connected
    assert "private-token" not in result.message


@pytest.mark.asyncio
async def test_missing_token_and_invalid_tool_arguments(monkeypatch, grafana, loki):
    monkeypatch.delenv("GRAFANA_TEST_TOKEN", raising=False)
    result = await ConnectorManager([grafana]).test_connection("grafana")
    assert not result.connected and "GRAFANA_TEST_TOKEN" in result.message
    server = ObservabilityServer(loki)
    with pytest.raises(ValueError, match="unknown read-only"):
        await server.call_tool("delete", {})
    for updates in [{"limit": 201}, {"tenant_id": "other"}, {"url": "http://other"}]:
        with pytest.raises(ValidationError):
            await server.call_tool(
                "loki_query_range",
                {
                    "query": '{app="billing"}',
                    "start": "1",
                    "end": "2",
                    **updates,
                },
            )
    assert ObservabilityServer(loki.model_copy(update={"tenant_id": None}))._headers() == {
        "Accept": "application/json"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [{"database": "bad"}, {"status": "ERROR"}, {"status": "success", "data": {}}]
)
async def test_semantic_health_failure(monkeypatch, grafana, payload):
    monkeypatch.setenv("GRAFANA_TEST_TOKEN", "test")

    def response(request):
        if request.url.path == "/api/health":
            data = payload if "database" in payload else {"database": "ok"}
        elif request.url.path.endswith("/health"):
            data = payload if payload.get("status") == "ERROR" else {"status": "OK"}
        else:
            data = payload
        return httpx.Response(200, json=data)

    mock_http(monkeypatch, response)
    assert not (await ConnectorManager([grafana]).test_connection("grafana")).connected


def test_config_validation_and_roundtrip(tmp_path, loki, grafana):
    config = Config(connectors=[loki, grafana])
    path = tmp_path / "config.toml"
    save_config(config, path)
    assert load_config(path) == config
    for updates in [
        {"url": "file:///tmp/logs"},
        {"url": "http://user:secret@host"},
        {"url": "http://host?token=secret"},
        {"tenant_id": "a|b"},
        {"tenant_id": "\r\n"},
        {"tenant_id": ""},
    ]:
        with pytest.raises(ValidationError):
            ConnectorConfig.model_validate({**loki.model_dump(), **updates})
    with pytest.raises(ValidationError, match="datasource_uid"):
        ConnectorConfig(name="grafana", type="grafana", url="http://grafana")


@pytest.mark.asyncio
async def test_live_mcp_probe_does_not_trust_startup_or_cache():
    session = Mock(invalidate_tools_cache=Mock(), list_tools=AsyncMock(), cleanup=AsyncMock())
    del session.check_health
    config = ConnectorConfig(name="logs", type="mcp", url="http://mcp", capabilities=["logs"])
    manager = ConnectorManager([config], {"logs": AsyncMock(return_value=session)})
    await manager.start()
    assert (await manager.health())["logs"].connected
    session.list_tools.side_effect = RuntimeError("offline")
    assert not (await manager.health())["logs"].connected
    session.list_tools.side_effect = None
    manager.errors["logs"] = "old startup error"
    assert (await manager.health())["logs"].connected
    assert session.invalidate_tools_cache.call_count == 3
    await manager.stop()


@pytest.mark.asyncio
async def test_probe_timeout_and_webhook_gap(monkeypatch, loki):
    manager = ConnectorManager([loki, ConnectorConfig(name="grafana", type="webhook")])
    manager.test_connection = AsyncMock(side_effect=TimeoutError)
    assert not (await manager.health())["loki"].connected
    empty = ConnectorManager([ConnectorConfig(name="grafana", type="webhook")])
    assert "no configured log-query" in (await empty.health())["observability"].message


def test_health_detects_outage_and_recovery(monkeypatch, tmp_path, loki):
    path = tmp_path / "config.toml"
    save_config(Config(runtime_root=tmp_path / "runtime", connectors=[loki]), path)
    application = Application.build(path, agent_backend=AsyncMock())
    current_status = [200]
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            current_status[0],
            json={"status": "success", "data": []},
        ),
    )
    with TestClient(create_server(application, run_worker=False)) as client:
        assert client.get("/health").json()["connections"]["loki"]["status"] == "ok"
        current_status[0] = 401
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["connections"]["loki"]["error"] == "loki: HTTP 401"
        current_status[0] = 200
        assert client.get("/health").status_code == 200


@pytest.mark.asyncio
async def test_github_probes_repo_pr_and_git_access(tmp_path, monkeypatch):
    config = Config(
        repositories=[
            RepositoryConfig(
                name="owner/repo",
                clone_url="https://github.com/owner/repo.git",
            )
        ]
    )
    adapter = GitHubCLIAdapter(config, Storage(tmp_path))
    command = AsyncMock(return_value=json.dumps({"permissions": {"push": True}}))
    monkeypatch.setattr(adapter, "_health_command", command)
    assert (await adapter.check_health())["github:owner/repo"]["status"] == "ok"
    assert command.await_count == 3
    command.return_value = json.dumps({"permissions": {"push": False}})
    assert (await adapter.check_health())["github:owner/repo"]["status"] == "failed"
    command.side_effect = TimeoutError
    assert "timed out" in (await adapter.check_health())["github:owner/repo"]["error"]
    command.side_effect = RuntimeError("gh connection/access check failed")
    assert (await adapter.check_health())["github:owner/repo"]["status"] == "failed"
    config.repositories[0].publish_mode = "local"
    assert await adapter.check_health() == {}


@pytest.mark.asyncio
async def test_github_health_command_reaps_timeout_and_redacts_errors(monkeypatch):
    process = Mock(
        returncode=0, communicate=AsyncMock(return_value=(b"result", b"")), wait=AsyncMock()
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    assert await GitHubCLIAdapter._health_command("gh", "api", "user") == "result"
    process.returncode = 1
    process.communicate.return_value = (b"", b"secret")
    with pytest.raises(RuntimeError, match="gh connection/access check failed"):
        await GitHubCLIAdapter._health_command("gh")
    process.communicate.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        await GitHubCLIAdapter._health_command("gh")
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()


def test_health_includes_github_failure_and_recovery(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    save_config(
        Config(
            runtime_root=tmp_path / "runtime",
            repositories=[
                RepositoryConfig(
                    name="owner/repo",
                    publish_mode="github",
                )
            ],
        ),
        path,
    )
    application = Application.build(path, agent_backend=AsyncMock())
    probe = AsyncMock(
        return_value={
            "github:owner/repo": {
                "status": "failed",
                "error": "gh connection/access check failed",
            }
        }
    )
    monkeypatch.setattr(application.github.api, "check_health", probe)
    with TestClient(create_server(application, run_worker=False)) as client:
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["connections"]["github:owner/repo"]["status"] == "failed"
        probe.return_value = {"github:owner/repo": {"status": "ok"}}
        assert client.get("/health").status_code == 200


@pytest.mark.asyncio
async def test_subscription_bridge_returns_structured_log_evidence(tmp_path, loki, monkeypatch):
    from src.agent import AgentRunContext, SubscriptionCLIBackend
    from src.models import Incident
    from src.tools import WorkspaceTools

    config = Config(runtime_root=tmp_path / "runtime", connectors=[loki])
    storage = Storage(config.runtime_root)
    task = storage.create_task(
        Incident(
            external_id="logs-test",
            source="grafana",
            repository="owner/repo",
            environment="production",
            summary="billing error",
        )
    )
    server = ObservabilityServer(loki)
    mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"result": [{"values": [["123", "test exception"]]}]},
            },
        ),
    )
    context = AgentRunContext(
        task=task,
        session_id=task.task_id,
        session_db=tmp_path / "session.db",
        lifecycle=Mock(),
        save_backend_session=Mock(),
        memory_writer=Mock(),
        capabilities=frozenset({"logs"}),
        connector_tools=(server,),
    )
    backend = SubscriptionCLIBackend(config)
    help_text = await backend._connector_help(context)
    assert "loki_query_range" in help_text and '"start"' in help_text
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    workspace = WorkspaceTools(worktree, permissions=config.permissions)
    async with backend._tool_bridge(context, workspace):
        child = await asyncio.create_subprocess_exec(
            str(worktree / "harness-out" / "incident-session-tool"),
            "connector_call",
            json.dumps(
                {
                    "connector": "loki",
                    "tool": "loki_query_range",
                    "arguments": {
                        "query": '{app="billing"}',
                        "start": "1",
                        "end": "2",
                    },
                }
            ),
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await child.communicate()
        result = json.loads(stdout)
        assert result["ok"]
        assert "test exception" in result["result"]["content"][0]["text"]
