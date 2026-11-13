from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from src.config import ConnectorConfig
from src.connectors import ConnectorManager


def manager(url: str | None, auth_token_env: str | None = None) -> ConnectorManager:
    config = ConnectorConfig(
        name="receiver", type="webhook", url=url, auth_token_env=auth_token_env
    )
    return ConnectorManager([config])


def mock_client(monkeypatch, handler):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def create_client(**kwargs):
        return original(transport=transport, **kwargs)

    monkeypatch.setattr("src.connectors.httpx.AsyncClient", create_client)


@pytest.mark.asyncio
async def test_redirect_is_reachability_only_and_does_not_forward_auth(monkeypatch) -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://elsewhere.example/secret"})

    mock_client(monkeypatch, respond)
    monkeypatch.setenv("WEBHOOK_TOKEN", "top-secret")
    result = await manager("https://receiver.example/hook", "WEBHOOK_TOKEN").test_connection(
        "receiver"
    )

    assert result.connected
    assert "HTTP 302" in result.message
    assert "delivery not verified" in result.message
    assert "top-secret" not in result.message
    assert "elsewhere.example" not in result.message
    assert len(requests) == 1
    assert requests[0].headers["authorization"] == "Bearer top-secret"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 429])
async def test_client_error_response_is_failure_without_leaking_response_data(
    monkeypatch, status
) -> None:
    mock_client(
        monkeypatch,
        lambda request: httpx.Response(status, text="private upstream detail"),
    )
    result = await manager("https://receiver.example/hook").test_connection("receiver")

    assert not result.connected
    assert f"HTTP {status}" in result.message
    assert "private upstream detail" not in result.message
    assert "receiver.example" not in result.message


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, ""])
async def test_missing_or_empty_auth_environment_skips_request(monkeypatch, token) -> None:
    requests = []
    mock_client(monkeypatch, lambda request: requests.append(request) or httpx.Response(204))
    monkeypatch.delenv("WEBHOOK_TOKEN", raising=False)
    if token is not None:
        monkeypatch.setenv("WEBHOOK_TOKEN", token)

    result = await manager("https://receiver.example/hook", "WEBHOOK_TOKEN").test_connection(
        "receiver"
    )

    assert not result.connected
    assert "WEBHOOK_TOKEN" in result.message
    assert "not set" in result.message
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [
        httpx.ConnectError("private detail"),
        httpx.ReadTimeout("private detail"),
    ],
)
async def test_network_failure_returns_safe_actionable_result(monkeypatch, error) -> None:
    def fail(request):
        raise error

    mock_client(monkeypatch, fail)
    result = await manager("https://receiver.example/hook").test_connection("receiver")

    assert not result.connected
    assert "check the URL and network connection" in result.message
    assert "private detail" not in result.message
    assert "receiver.example" not in result.message


@pytest.mark.parametrize(
    "url", [
        "ftp://receiver.example/hook",
        "https://user:password@receiver.example/hook",
        "https://receiver.example/hook?token=secret",
        "https://receiver.example/hook#fragment",
    ],
)
def test_webhook_rejects_unsupported_or_credential_bearing_urls(url) -> None:
    with pytest.raises(ValidationError):
        ConnectorConfig(name="receiver", type="webhook", url=url)


def test_webhook_without_url_remains_valid_for_inbound_configuration() -> None:
    config = ConnectorConfig(name="receiver", type="webhook")

    assert config.url is None
