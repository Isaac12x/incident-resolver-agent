from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.app import Application
from src.config import Config, ConnectorConfig, ServerConfig, save_config
from src.server import create_server


@pytest.fixture
def application(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Application:
    monkeypatch.delenv("INCIDENT_AGENT_API_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_WEBHOOK_SECRET", raising=False)
    path = tmp_path / "config.toml"
    save_config(
        Config(
            runtime_root=tmp_path / "state",
            server=ServerConfig(require_api_auth=True),
            connectors=[ConnectorConfig(name="grafana", type="webhook")],
        ),
        path,
    )
    return Application.build(path)


def test_required_missing_credentials_fail_closed(application: Application) -> None:
    client = TestClient(create_server(application, run_worker=False))
    for path in ("/mcp", "/mcp/resources/intelligence/events", "/a2a/tasks/missing"):
        assert client.get(path).status_code == 503
    assert client.post("/hooks/incidents/grafana", json={}).status_code == 503
    assert client.get("/.well-known/agent-card.json").status_code == 200


def test_bearer_auth_protects_reads_and_mutations(
    application: Application,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INCIDENT_AGENT_API_TOKEN", "private-token")
    client = TestClient(create_server(application, run_worker=False))
    paths = ["/mcp/resources/intelligence/events", "/a2a/tasks/missing"]
    for path in paths:
        for authorization in ("", "Basic private-token", "Bearer wrong"):
            response = client.get(path, headers={"Authorization": authorization})
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"
            assert "private-token" not in response.text
    assert client.post("/mcp/tools/rebuild_intelligence").status_code == 401
    assert client.post("/a2a/tasks/missing/cancel").status_code == 401
    assert client.get(paths[0], headers={"Authorization": "bearer private-token"}).json() == []
    assert (
        client.get(paths[1], headers={"Authorization": "Bearer private-token"}).status_code == 404
    )
    # Setting a credential also protects migrated deployments with the legacy flag.
    application.config.server.require_api_auth = False
    assert client.get(paths[0]).status_code == 401


def test_webhooks_retain_independent_hmac_auth(
    application: Application,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_WEBHOOK_SECRET", "hook-token")
    client = TestClient(create_server(application, run_worker=False))
    body = b'{"alerts":[{"status":"resolved","fingerprint":"1"}]}'
    assert client.post("/hooks/incidents/grafana", content=body).status_code == 401
    digest = hmac.new(b"hook-token", body, hashlib.sha256).hexdigest()
    response = client.post(
        "/hooks/incidents/grafana",
        content=body,
        headers={"x-agent-signature-256": "sha256=" + digest},
    )
    assert response.status_code == 202


def test_webhook_routes_reject_oversized_unsigned_bodies(application: Application) -> None:
    from src.server import MAX_WEBHOOK_BODY_BYTES

    client = TestClient(create_server(application, run_worker=False))
    for path in ("/hooks/incidents/grafana", "/hooks/github"):
        response = client.post(path, content=b"x" * (MAX_WEBHOOK_BODY_BYTES + 1))
        assert response.status_code == 413
    assert application.storage.list_observability_events() == []
