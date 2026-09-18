"""Disposable, local proxy configuration and live publication checks."""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path

import pytest

from src.dashboard import routing


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_port(port: int) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"temporary proxy did not listen on {port}")


def _upstream(root: Path):
    import uvicorn

    port = _free_port()
    from src.dashboard.web import create_dashboard

    app = create_dashboard(root, port=port, nonce="integration")
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", proxy_headers=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_port(port)
    return server, thread, port


def _https_request(port: int, method: str, path: str, headers: dict[str, str], body: bytes = b""):
    context = ssl._create_unverified_context()
    raw = socket.create_connection(("127.0.0.1", port), timeout=3)
    stream = context.wrap_socket(raw, server_hostname="incidents.example.test")
    request = [
        f"{method} {path} HTTP/1.1",
        f"Host: incidents.example.test:{port}",
        "Connection: close",
    ]
    request.extend(f"{key}: {value}" for key, value in headers.items())
    if body:
        request.append(f"Content-Length: {len(body)}")
    stream.sendall(("\r\n".join(request) + "\r\n\r\n").encode() + body)
    response = http.client.HTTPResponse(stream)
    response.begin()
    payload = response.read(16384)
    stream.close()
    return response.status, dict(response.getheaders()), payload


def _exercise_authenticated_flow(root: Path, public: int) -> None:
    token = (root / "dashboard/token").read_text(encoding="utf-8").strip()
    status, headers, _ = _https_request(
        public,
        "POST",
        "/login",
        {"Content-Type": "application/json"},
        json.dumps({"token": token}).encode(),
    )
    assert status == 200
    cookie_header = next(value for key, value in headers.items() if key.lower() == "set-cookie")
    cookie = cookie_header.split(";", 1)[0]
    status, _, body = _https_request(public, "GET", "/api/snapshot", {"Cookie": cookie})
    assert status == 200
    assert isinstance(json.loads(body), dict)

    context = ssl._create_unverified_context()
    stream = context.wrap_socket(
        socket.create_connection(("127.0.0.1", public), timeout=3),
        server_hostname="incidents.example.test",
    )
    stream.sendall(
        (
            f"GET /api/events HTTP/1.1\r\nHost: incidents.example.test:{public}\r\n"
            f"Cookie: {cookie}\r\nConnection: close\r\n\r\n"
        ).encode()
    )
    stream.settimeout(3)
    received = b""
    while b"event: snapshot" not in received and len(received) < 32768:
        received += stream.recv(4096)
    stream.close()
    assert b"HTTP/1.1 200" in received
    assert b"Content-Type: text/event-stream" in received
    assert b"event: snapshot" in received
    return cookie


def _assert_plain_route(port: int, expected: bytes) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=3) as stream:
        stream.sendall(
            f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode()
        )
        body = b""
        while True:
            chunk = stream.recv(4096)
            if not chunk:
                break
            body += chunk
    assert expected in body


def _args(config: Path, public: int, upstream: int, cert: Path, key: Path, proxy: str):
    import argparse

    return argparse.Namespace(
        target=str(public),
        host="incidents.example.test",
        proxy=proxy,
        proxy_config=config,
        tls_cert=cert,
        tls_key=key,
        upstream_port=upstream,
        route_path="/",
    )


def _cert_pair(tmp_path: Path) -> tuple[Path, Path]:
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=incidents.example.test",
            "-days",
            "1",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


@pytest.mark.skipif(not Path("/opt/homebrew/bin/caddy").exists(), reason="caddy is unavailable")
def test_caddy_real_binary_validates_explicit_tls_and_safety_headers(tmp_path: Path):
    cert, key = _cert_pair(tmp_path)
    snippet = tmp_path / "dashboard.conf"
    snippet.write_text(
        routing._snippet(
            "caddy",
            "incidents.example.test",
            18443,
            18766,
            "secret",
            str(cert),
            str(key),
        ),
        encoding="utf-8",
    )
    config = tmp_path / "Caddyfile"
    config.write_text(f'import "{snippet}"\n', encoding="utf-8")
    proxy = routing.Proxy("caddy", "/opt/homebrew/bin/caddy", config)
    routing._validate(proxy, config)
    text = snippet.read_text(encoding="utf-8")
    assert "header_up Host incidents.example.test:18443" in text
    assert "header_up X-Forwarded-Proto https" in text
    assert "header_up -Forwarded" in text
    assert "flush_interval -1" in text


@pytest.mark.skipif(not Path("/opt/homebrew/bin/nginx").exists(), reason="nginx is unavailable")
def test_nginx_real_binary_validates_disposable_tls_config(tmp_path: Path):
    cert, key = _cert_pair(tmp_path)
    snippet = tmp_path / "dashboard.conf"
    snippet.write_text(
        routing._snippet(
            "nginx",
            "incidents.example.test",
            18443,
            18766,
            "secret",
            str(cert),
            str(key),
        ),
        encoding="utf-8",
    )
    config = tmp_path / "nginx.conf"
    config.write_text(f'events {{}}\nhttp {{ include "{snippet}"; }}\n', encoding="utf-8")
    proxy = routing.Proxy("nginx", "/opt/homebrew/bin/nginx", config)
    routing._validate(proxy, config)
    text = snippet.read_text(encoding="utf-8")
    assert "proxy_set_header Host incidents.example.test:18443;" in text
    assert "proxy_buffering off;" in text


def test_bind_preflight_checks_wildcard_listener(tmp_path: Path):
    import socket

    sock = socket.socket()
    sock.bind(("0.0.0.0", 0))
    port = sock.getsockname()[1]
    try:
        assert routing._bind_occupied(port)
    finally:
        sock.close()


@pytest.mark.skipif(not Path("/opt/homebrew/bin/caddy").exists(), reason="caddy is unavailable")
def test_caddy_live_open_reload_probe_and_close(tmp_path: Path):
    cert, key = _cert_pair(tmp_path)
    admin = _free_port()
    public = _free_port()
    upstream_server, upstream_thread, upstream = _upstream(tmp_path)
    config = tmp_path / "Caddyfile"
    base = _free_port()
    config.write_text(
        "{\n"
        f"    admin localhost:{admin}\n"
        "    persist_config off\n"
        "    auto_https disable_redirects\n"
        "}\n"
        f'http://127.0.0.1:{base} {{\n    respond "base" 200\n}}\n',
        encoding="utf-8",
    )
    original_snippet = routing._snippet

    def loopback_caddy(*snippet_args, **snippet_kwargs):
        text = original_snippet(*snippet_args, **snippet_kwargs)
        host, port = snippet_args[1], snippet_args[2]
        return text.replace(f"{host}:{port} {{\n", f"{host}:{port} {{\n    bind 127.0.0.1\n")

    # Keep the disposable proxy listener loopback-only; production snippets remain public.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(routing, "_snippet", loopback_caddy)
    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg-config")
    env["XDG_DATA_HOME"] = str(tmp_path / "xdg-data")
    process = subprocess.Popen(
        ["/opt/homebrew/bin/caddy", "run", "--config", str(config), "--adapter", "caddyfile"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    try:
        _wait_port(admin)
        _wait_port(base)
        _assert_plain_route(base, b"base")
        args = _args(config, public, upstream, cert, key, "caddy")
        assert routing.run_port_command(args, tmp_path) == 0
        public_state = __import__("json").loads((tmp_path / "dashboard/public.json").read_text())
        assert public_state["enabled"] is True
        assert public_state["scheme"] == "https"
        cookie = _exercise_authenticated_flow(tmp_path, public)
        args.target = "close"
        assert routing.run_port_command(args, tmp_path) == 0
        assert (
            __import__("json").loads((tmp_path / "dashboard/public.json").read_text())["enabled"]
            is False
        )
        _assert_plain_route(base, b"base")
        with pytest.raises(OSError):
            _https_request(public, "GET", "/api/snapshot", {"Cookie": cookie})
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        upstream_server.should_exit = True
        upstream_thread.join(timeout=5)
        monkeypatch.undo()


@pytest.mark.skipif(not Path("/opt/homebrew/bin/nginx").exists(), reason="nginx is unavailable")
def test_nginx_live_open_reload_probe_and_close(tmp_path: Path):
    cert, key = _cert_pair(tmp_path)
    public = _free_port()
    base = _free_port()
    upstream_server, upstream_thread, upstream = _upstream(tmp_path)
    config = tmp_path / "nginx.conf"
    config.write_text(
        f"pid {tmp_path / 'nginx.pid'};\n"
        f"error_log {tmp_path / 'nginx.error.log'};\n"
        "events {}\n"
        f"http {{ access_log off; server {{ listen 127.0.0.1:{base}; "
        "return 200 'base'; } }\n",
        encoding="utf-8",
    )
    original_snippet = routing._snippet

    def loopback_nginx(*snippet_args, **snippet_kwargs):
        text = original_snippet(*snippet_args, **snippet_kwargs)
        port = snippet_args[2]
        return text.replace(
            f"    listen {port} ssl;\n    listen [::]:{port} ssl;",
            f"    listen 127.0.0.1:{port} ssl;",
        )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(routing, "_snippet", loopback_nginx)
    subprocess.run(
        ["/opt/homebrew/bin/nginx", "-p", str(tmp_path) + "/", "-c", str(config)],
        check=True,
        capture_output=True,
    )
    try:
        _wait_port(base)
        _assert_plain_route(base, b"base")
        args = _args(config, public, upstream, cert, key, "nginx")
        assert routing.run_port_command(args, tmp_path) == 0
        assert (
            __import__("json").loads((tmp_path / "dashboard/public.json").read_text())["enabled"]
            is True
        )
        cookie = _exercise_authenticated_flow(tmp_path, public)
        args.target = "close"
        assert routing.run_port_command(args, tmp_path) == 0
        assert (
            __import__("json").loads((tmp_path / "dashboard/public.json").read_text())["enabled"]
            is False
        )
        _assert_plain_route(base, b"base")
        with pytest.raises(OSError):
            _https_request(public, "GET", "/api/snapshot", {"Cookie": cookie})
    finally:
        subprocess.run(
            ["/opt/homebrew/bin/nginx", "-p", str(tmp_path) + "/", "-s", "quit", "-c", str(config)],
            check=False,
            capture_output=True,
        )
        upstream_server.should_exit = True
        upstream_thread.join(timeout=5)
        monkeypatch.undo()
