"""Regression coverage for the dashboard HTTP boundary and live stream."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

import src.dashboard.web as web
from src.dashboard.web import _Access, _Poller, create_dashboard


def make_root(root: Path, token: str | None = "dashboard-token") -> Path:
    directory = root / "dashboard"
    directory.mkdir(parents=True, exist_ok=True)
    if token is not None:
        (directory / "token").write_text(token + "\n", encoding="utf-8")
    return root


def local_client(root: Path, *, port: int = 8766, nonce: str = "instance") -> TestClient:
    return TestClient(
        create_dashboard(root, port=port, nonce=nonce), base_url=f"http://127.0.0.1:{port}"
    )


def public_config(root: Path, *, enabled: bool = True, generation: int = 1) -> None:
    (root / "dashboard" / "public.json").write_text(
        json.dumps(
            {
                "enabled": enabled,
                "generation": generation,
                "upstream_secret": "proxy-secret",
                "probe_secret": "health-probe-secret",
                "hosts": ["incidents.example.test"],
                "public_port": 8443,
                "scheme": "https",
            }
        ),
        encoding="utf-8",
    )


def public_client(root: Path) -> tuple[TestClient, dict[str, str]]:
    client = TestClient(
        create_dashboard(root, port=8766, nonce="instance"),
        base_url="https://incidents.example.test:8443",
    )
    return client, {
        "x-dashboard-upstream-secret": "proxy-secret",
        "x-forwarded-proto": "https",
    }


def route_endpoint(app: Any, path: str):
    return next(route.endpoint for route in app.routes if getattr(route, "path", None) == path)


def request_for(
    path: str,
    *,
    host: str = "127.0.0.1:8766",
    headers: dict[str, str] | None = None,
    cookies: str = "",
    body: list[bytes] | None = None,
    access: _Access | None = None,
) -> Request:
    access = access or _Access("local")
    supplied = {"host": host, **(headers or {})}
    if cookies:
        supplied["cookie"] = cookies
    encoded_headers = [(key.lower().encode(), value.encode()) for key, value in supplied.items()]
    chunks = iter(body or [b""])

    async def receive() -> dict[str, Any]:
        try:
            chunk = next(chunks)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": chunk, "more_body": bool(chunk)}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https" if access.scope == "public" else "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": encoded_headers,
        "client": ("testclient", 50000),
        "server": ("127.0.0.1", 8766),
    }
    request = Request(scope, receive)
    request.state.dashboard_access = access
    return request


def test_local_login_snapshot_logout_and_readiness(tmp_path: Path):
    root = make_root(tmp_path)
    client = local_client(root)
    assert client.get("/").status_code == 200
    assert client.get("/health").json() == {
        "ok": True,
        "nonce": "instance",
        "public_enabled": False,
        "public_generation": None,
    }
    assert client.get("/api/snapshot").status_code == 401
    login = client.post("/login", json={"token": "dashboard-token"})
    assert login.status_code == 200
    assert "dashboard_local_session=" in login.headers["set-cookie"]
    assert "Secure" not in login.headers["set-cookie"]
    assert client.get("/api/snapshot").status_code == 200
    assert client.post("/logout").status_code == 200
    assert client.get("/api/snapshot").status_code == 401


def test_public_cookie_contract_and_generation_revoke(tmp_path: Path):
    root = make_root(tmp_path)
    public_config(root)
    client, headers = public_client(root)
    login = client.post("/login", headers=headers, json={"token": "dashboard-token"})
    assert login.status_code == 200
    assert "dashboard_public_session=" in login.headers["set-cookie"]
    assert "Secure" in login.headers["set-cookie"]
    assert client.get("/api/snapshot", headers=headers).status_code == 200
    public_config(root, enabled=False, generation=2)
    assert client.get("/api/snapshot", headers=headers).status_code == 403
    public_config(root, generation=3)
    assert client.get("/api/snapshot", headers=headers).status_code == 401
    assert (
        client.post("/login", headers=headers, json={"token": "dashboard-token"}).status_code
        == 200
    )


def test_strict_host_origin_and_forwarded_headers(tmp_path: Path):
    root = make_root(tmp_path)
    client = local_client(root)
    assert client.get("/health", headers={"host": "evil.example"}).status_code == 400
    assert client.get("/health", headers={"host": "127.0.0.1:8767"}).status_code == 400
    assert client.get("/health", headers={"origin": "http://evil.example"}).status_code == 400
    assert client.get("/health", headers={"x-forwarded-proto": "https"}).status_code == 400
    assert (
        client.get(
            "/health", headers={"x-dashboard-probe-secret": "health-probe-secret"}
        ).status_code
        == 400
    )
    public_config(root)
    public, headers = public_client(root)
    assert (
        public.get("/health", headers={**headers, "host": "other.example:8443"}).status_code
        == 400
    )
    assert (
        public.get("/health", headers={**headers, "x-forwarded-for": "1.2.3.4"}).status_code
        == 400
    )
    assert (
        public.get("/health", headers={**headers, "origin": "https://other.example:8443"}).status_code
        == 400
    )
    assert public.get(
        "/health",
        headers={"x-dashboard-upstream-secret": "wrong", "x-forwarded-proto": "https"},
    ).status_code == 400
    assert public.get(
        "/health", headers={"x-dashboard-upstream-secret": "proxy-secret"}
    ).status_code == 400


def test_public_https_default_origin_port_is_normalized(tmp_path: Path):
    root = make_root(tmp_path)
    public_config(root)
    payload = json.loads((root / "dashboard" / "public.json").read_text(encoding="utf-8"))
    payload["public_port"] = 443
    (root / "dashboard" / "public.json").write_text(json.dumps(payload), encoding="utf-8")
    client, proxy_headers = public_client(root)
    headers = {
        **proxy_headers,
        "host": "incidents.example.test:443",
        "origin": "https://incidents.example.test",
    }
    assert client.get("/health", headers=headers).status_code == 200
    assert (
        client.post("/login", headers=headers, json={"token": "dashboard-token"}).status_code
        == 200
    )
    assert client.get(
        "/health", headers={**headers, "origin": "https://incidents.example.test:8443"}
    ).status_code == 400
    assert client.get(
        "/health", headers={**headers, "origin": "https://other.example.test"}
    ).status_code == 400


def test_disabled_or_malformed_public_config_fails_closed(tmp_path: Path):
    root = make_root(tmp_path)
    public_config(root, enabled=False)
    app = create_dashboard(root, nonce="probe-instance")
    client = TestClient(app, base_url="https://incidents.example.test:8443")
    headers = {"x-dashboard-upstream-secret": "proxy-secret", "x-forwarded-proto": "https"}
    assert client.get("/health", headers=headers).status_code == 403
    probe_headers = {
        **headers,
        "x-dashboard-probe-secret": "health-probe-secret",
    }
    probe = client.get("/health", headers=probe_headers)
    assert probe.status_code == 200
    assert probe.json()["nonce"] == "probe-instance"
    assert client.get("/api/snapshot", headers=probe_headers).status_code == 400
    assert (
        client.post("/login", headers=probe_headers, json={"token": "dashboard-token"}).status_code
        == 400
    )
    assert client.get(
        "/health", headers={**probe_headers, "x-dashboard-probe-secret": "wrong"}
    ).status_code == 400
    (root / "dashboard" / "public.json").write_text("{bad", encoding="utf-8")
    assert client.get("/health", headers=headers).status_code == 400
    (root / "dashboard" / "public.json").write_text(
        json.dumps({"enabled": True, "generation": 1, "upstream_secret": "x"}), encoding="utf-8"
    )
    assert client.get("/health", headers=headers).status_code == 400
    (root / "dashboard" / "public.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "generation": 1,
                "upstream_secret": "proxy-secret",
                "hosts": "not-a-list",
                "public_port": 8443,
                "scheme": "https",
            }
        ),
        encoding="utf-8",
    )
    assert client.get("/health", headers=headers).status_code == 400


def test_instances_do_not_share_sessions_and_expired_session_is_rejected(tmp_path: Path):
    root = make_root(tmp_path)
    app1 = create_dashboard(root, nonce="same")
    app2 = create_dashboard(root, nonce="same")
    client1 = TestClient(app1, base_url="http://127.0.0.1:8766")
    client2 = TestClient(app2, base_url="http://127.0.0.1:8766")
    assert client1.post("/login", json={"token": "dashboard-token"}).status_code == 200
    client2.cookies.update(client1.cookies)
    assert client2.get("/api/snapshot").status_code == 401
    session_id = next(iter(app1.state.sessions))
    app1.state.sessions[session_id] = ("local", None, app1.state.instance_id, time.time() - 1)
    assert client1.get("/api/snapshot").status_code == 401


def test_empty_missing_unicode_and_invalid_login_input(tmp_path: Path):
    root = make_root(tmp_path, token="")
    client = local_client(root)
    assert client.post("/login", json={"token": ""}).status_code == 401
    missing = tmp_path / "missing"
    generated = local_client(missing)
    token = (missing / "dashboard/token").read_text().strip()
    assert token and generated.post("/login", json={"token": token}).status_code == 200
    root2 = make_root(tmp_path / "unicode", token="tökén")
    unicode_client = local_client(root2)
    assert unicode_client.post("/login", json={"token": "tökén"}).status_code == 200
    assert unicode_client.post(
        "/login", content=b"\xff", headers={"content-type": "application/json"}
    ).status_code == 422
    assert unicode_client.post(
        "/login", content="{bad", headers={"content-type": "application/json"}
    ).status_code == 422


def test_login_rate_session_and_sse_client_limits(tmp_path: Path, monkeypatch):
    root = make_root(tmp_path)
    app = create_dashboard(root)
    client = TestClient(app, base_url="http://127.0.0.1:8766")
    for _ in range(10):
        assert client.post("/login", json={"token": "wrong"}).status_code == 401
    assert client.post("/login", json={"token": "wrong"}).status_code == 429
    monkeypatch.setattr(web, "_LOGIN_WINDOW", 0)
    assert client.post("/login", json={"token": "dashboard-token"}).status_code == 200
    monkeypatch.setattr(web, "_SESSION_LIMIT", 1)
    client2 = TestClient(app, base_url="http://127.0.0.1:8766")
    assert client2.post("/login", json={"token": "dashboard-token"}).status_code == 200
    assert len(app.state.sessions) == 1
    monkeypatch.setattr(web, "_SSE_LIMIT", 0)
    assert client2.get("/api/events").status_code == 503


def test_streamed_body_limit_without_content_length(tmp_path: Path):
    root = make_root(tmp_path)
    app = create_dashboard(root)
    endpoint = route_endpoint(app, "/login")
    request = request_for(
        "/login", headers={"content-type": "text/plain"}, body=[b"x" * 2000, b"y" * 2100]
    )
    with pytest.raises(HTTPException, match="body too large") as error:
        asyncio.run(endpoint(request))
    assert error.value.status_code == 413


def test_snapshot_query_bounds_dates_and_unavailable_database(tmp_path: Path):
    root = make_root(tmp_path)
    client = local_client(root)
    assert client.post("/login", json={"token": "dashboard-token"}).status_code == 200
    assert client.get("/api/snapshot?page=0").status_code == 422
    assert client.get("/api/snapshot?page_size=101").status_code == 422
    assert client.get("/api/snapshot?since=not-a-date").status_code == 422
    assert client.get("/api/snapshot?until=2026-99-99").status_code == 422
    payload = client.get("/api/snapshot").json()
    assert payload["available"] is False
    assert payload["tasks"] == []


def test_asset_allowlist_and_mime_types(tmp_path: Path):
    root = make_root(tmp_path)
    client = local_client(root)
    assert client.get("/assets/app.js").headers["content-type"].startswith("text/javascript")
    assert client.get("/assets/style.css").headers["content-type"].startswith("text/css")
    assert client.get("/assets/index.html").headers["content-type"].startswith("text/html")
    assert client.get("/assets/../web.py").status_code == 404
    assert client.get("/assets/missing.js").status_code == 404


def test_poller_invalidates_same_count_task_event_and_metric_changes(tmp_path: Path):
    class Reader:
        path = tmp_path / "runtime.sqlite3"

    poller = _Poller(Reader())
    (tmp_path / "runtime.sqlite3").write_bytes(b"db")
    first = {
        "available": True,
        "tasks": [{"id": "a", "state": "active"}],
        "summary": {"active": 1},
        "metrics": [{"calls": 1}],
    }
    task_change = {**first, "tasks": [{"id": "a", "state": "waiting"}]}
    event_change = {**first, "tasks": [{"id": "a", "state": "active", "event_sequence": 2}]}
    metric_change = {**first, "metrics": [{"calls": 2}]}
    markers = {
        poller._snapshot_marker(item)
        for item in (first, task_change, event_change, metric_change)
    }
    assert len(markers) == 4
    queue = poller.subscribe()
    poller._publish({"snapshot": task_change})
    assert queue.get_nowait()["snapshot"] == task_change
    poller.unsubscribe(queue)


def test_poller_full_queue_evicts_slow_client_and_bounds_subscriptions(monkeypatch):
    poller = _Poller(object())
    queue = poller.subscribe()
    queue.put_nowait({"old": 1})
    queue.put_nowait({"old": 2})
    poller._publish({"new": 3})
    assert queue.get_nowait() is None
    poller.unsubscribe(queue)
    monkeypatch.setattr(web, "_SSE_LIMIT", 1)
    poller.subscribe()
    with pytest.raises(HTTPException, match="capacity"):
        poller.subscribe()


def test_sse_generator_reconnect_reset_and_revocation_cleanup(tmp_path: Path):
    root = make_root(tmp_path)
    public_config(root)
    app = create_dashboard(root, nonce="instance")
    client = TestClient(app, base_url="https://incidents.example.test:8443")
    headers = {"x-dashboard-upstream-secret": "proxy-secret", "x-forwarded-proto": "https"}
    assert (
        client.post("/login", headers=headers, json={"token": "dashboard-token"}).status_code
        == 200
    )
    session = client.cookies.get("dashboard_public_session")
    cookie = f"dashboard_public_session={session}"

    async def exercise() -> tuple[bytes, bool]:
        endpoint = route_endpoint(app, "/api/events")
        request = request_for(
            "/api/events",
            host="incidents.example.test:8443",
            headers={**headers, "last-event-id": "4"},
            cookies=cookie,
            access=_Access("public", "1"),
        )
        response = await endpoint(request)
        iterator = response.body_iterator
        initial = await anext(iterator)
        assert app.state.poller.clients
        public_config(root, enabled=False, generation=2)
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
        await response.body_iterator.aclose()
        return initial, not app.state.poller.clients

    initial, cleaned = asyncio.run(exercise())
    assert '"reset":true' in initial
    assert cleaned


def test_logout_requires_auth(tmp_path: Path):
    root = make_root(tmp_path)
    client = local_client(root)
    assert client.post("/logout").status_code == 401


def test_task_endpoint_and_plain_login_body_errors(tmp_path: Path):
    root = make_root(tmp_path)
    app = create_dashboard(root)
    client = TestClient(app, base_url="http://127.0.0.1:8766")
    assert client.post("/login", json={"token": "dashboard-token"}).status_code == 200
    app.state.reader.task = lambda task_id: {"id": task_id, "events": []}
    assert client.get("/api/tasks/task-1").json()["id"] == "task-1"
    app.state.reader.task = lambda task_id: None
    assert client.get("/api/tasks/missing").status_code == 404
    assert (
        client.post("/login", content=b"\xff", headers={"content-type": "text/plain"}).status_code
        == 401
    )
    assert client.post(
        "/login", content=b"x", headers={"content-length": "5000", "content-type": "text/plain"}
    ).status_code == 413


def test_stream_updates_and_heartbeat_are_observable(tmp_path: Path, monkeypatch):
    root = make_root(tmp_path)
    app = create_dashboard(root)
    client = TestClient(app, base_url="http://127.0.0.1:8766")
    assert client.post("/login", json={"token": "dashboard-token"}).status_code == 200

    async def exercise() -> tuple[str, str]:
        endpoint = route_endpoint(app, "/api/events")
        session = client.cookies.get("dashboard_local_session")
        request = request_for("/api/events", cookies=f"dashboard_local_session={session}")
        response = await endpoint(request)
        iterator = response.body_iterator
        await anext(iterator)
        monkeypatch.setattr(web, "_POLL_INTERVAL", 0.001)
        heartbeat = await anext(iterator)
        app.state.poller._publish({"snapshot": {"available": True, "summary": {"active": 1}}})
        update = await anext(iterator)
        await response.body_iterator.aclose()
        return heartbeat, update

    heartbeat, update = asyncio.run(exercise())
    assert heartbeat == ": heartbeat\n\n"
    assert '"active":1' in update


def test_poller_lifecycle_and_retry_after_reader_error(monkeypatch):
    class Reader:
        path = None
        calls = 0

        def snapshot(self):
            self.calls += 1
            if self.calls == 1:
                raise OSError("database replaced")
            return {"available": True, "tasks": [], "summary": {}, "metrics": []}

    async def exercise() -> None:
        poller = _Poller(Reader())
        monkeypatch.setattr(web, "_POLL_INTERVAL", 0.001)
        await poller.start()
        await asyncio.sleep(0.01)
        await poller.stop()
        assert poller.task is not None and poller.task.done()
        await poller.start()
        poller.closed = False
        await poller.stop()
        closed = _Poller(Reader())
        closed.closed = True
        await closed.start()

    asyncio.run(exercise())


def test_poller_run_publishes_changed_snapshot(monkeypatch):
    class Reader:
        path = None

        def __init__(self):
            self.calls = 0

        def snapshot(self):
            self.calls += 1
            return {
                "available": True,
                "tasks": [{"state": "active" if self.calls == 1 else "failed"}],
                "summary": {},
                "metrics": [],
            }

    async def exercise() -> dict[str, Any]:
        poller = _Poller(Reader())
        queue = poller.subscribe()
        monkeypatch.setattr(web, "_POLL_INTERVAL", 0.001)
        await poller.start()
        for _ in range(20):
            await asyncio.sleep(0.001)
            if not queue.empty():
                break
        message = queue.get_nowait()
        await poller.stop()
        return message

    assert asyncio.run(exercise())["kind"] == "snapshot"


def test_malformed_host_and_asset_read_failure(tmp_path: Path, monkeypatch):
    root = make_root(tmp_path)
    client = local_client(root)
    assert client.get("/health", headers={"host": "[::1]:bad"}).status_code == 400
    assert web._host_without_port("[") == ""
    with pytest.raises(HTTPException, match="asset not found"):
        original = web.Path.read_text
        monkeypatch.setattr(
            web.Path,
            "read_text",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("gone")),
        )
        web._asset("style.css")
        monkeypatch.setattr(web.Path, "read_text", original)


def test_login_attempt_key_bound_and_lifespan_cleanup(tmp_path: Path, monkeypatch):
    root = make_root(tmp_path)
    app = create_dashboard(root)
    monkeypatch.setattr(web, "_LOGIN_KEY_LIMIT", 0)
    client = TestClient(app, base_url="http://127.0.0.1:8766")
    assert client.post("/login", json={"token": "wrong"}).status_code == 401
    with TestClient(app, base_url="http://127.0.0.1:8766") as managed:
        assert managed.get("/health").status_code == 200


def test_session_expiry_and_malformed_content_length(tmp_path: Path, monkeypatch):
    root = make_root(tmp_path)
    app = create_dashboard(root)
    client = TestClient(app, base_url="http://127.0.0.1:8766")
    assert client.post("/login", json={"token": "dashboard-token"}).status_code == 200
    session_id = next(iter(app.state.sessions))
    app.state.sessions[session_id] = ("local", None, app.state.instance_id, 100.5)
    clock = iter((100.0, 101.0, *([200.0] * 20)))
    monkeypatch.setattr(web.time, "time", lambda: next(clock))
    assert client.get("/api/snapshot").status_code == 401
    app2 = create_dashboard(make_root(tmp_path / "body"))
    endpoint = route_endpoint(app2, "/login")
    request = request_for(
        "/login",
        headers={"content-length": "not-a-number", "content-type": "text/plain"},
        body=[b"wrong"],
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(endpoint(request))
    assert error.value.status_code == 401
