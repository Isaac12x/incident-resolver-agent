"""Authenticated, read-only dashboard HTTP application."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from .data import RuntimeReader

_BODY_LIMIT = 4096
_SESSION_LIMIT = 1000
_SESSION_TTL = 8 * 60 * 60
_LOGIN_WINDOW = 60.0
_LOGIN_LIMIT = 10
_LOGIN_KEY_LIMIT = 2048
_SSE_LIMIT = 100
_POLL_INTERVAL = 2.0
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_FORWARDED_NAMES = {
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-port",
    "x-forwarded-proto",
    "x-real-ip",
}


@dataclass(frozen=True)
class _Access:
    scope: str
    public_generation: str | None = None


def _token(path: Path) -> str | None:
    """Read a token, generating only for a missing file; empty files never authenticate."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        value = ""
    except OSError:
        return None
    if path.exists():
        return value or None
    value = secrets.token_urlsafe(32)
    try:
        os_module = __import__("os")
        fd = os_module.open(path, os_module.O_CREAT | os_module.O_EXCL | os_module.O_WRONLY, 0o600)
        with os_module.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
    except FileExistsError:
        with suppress(OSError):
            value = path.read_text(encoding="utf-8").strip()
        return value or None
    return value


def _header_names(request: Request) -> set[str]:
    return {key.lower() for key in request.headers}


def _host_without_port(host: str) -> str:
    try:
        return (urlsplit(f"//{host}").hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _origin_matches(origin: str, host_header: str, scheme: str) -> bool:
    try:
        parsed_origin = urlsplit(origin)
        parsed_host = urlsplit(f"//{host_header}")
        if parsed_origin.scheme != scheme:
            return False
        origin_port = parsed_origin.port or (443 if scheme == "https" else 80)
        host_port = parsed_host.port or (443 if scheme == "https" else 80)
        return (
            (parsed_origin.hostname or "").lower().rstrip(".")
            == (parsed_host.hostname or "").lower().rstrip(".")
            and origin_port == host_port
        )
    except ValueError:
        return False


class _Poller:
    """One bounded reader poller shared by all browser streams in this app."""

    def __init__(self, reader: RuntimeReader) -> None:
        self.reader = reader
        self.clients: set[asyncio.Queue[dict[str, Any] | None]] = set()
        self.task: asyncio.Task[None] | None = None
        self.closed = False
        self._marker: str | None = None

    async def start(self) -> None:
        if self.closed:
            return
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name="dashboard-reader-poller")

    async def stop(self) -> None:
        self.closed = True
        for queue in tuple(self.clients):
            with suppress(asyncio.QueueFull):
                queue.put_nowait(None)
        self.clients.clear()
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    def subscribe(self) -> asyncio.Queue[dict[str, Any] | None]:
        if len(self.clients) >= _SSE_LIMIT:
            raise HTTPException(503, "dashboard event stream capacity reached")
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=2)
        self.clients.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        self.clients.discard(queue)

    async def _run(self) -> None:
        while not self.closed:
            try:
                snapshot = await asyncio.to_thread(self.reader.snapshot)
                marker = self._snapshot_marker(snapshot)
                if self._marker is not None and marker != self._marker:
                    self._publish({"kind": "snapshot", "snapshot": snapshot})
                self._marker = marker
            except Exception:
                # Replacements can briefly remove the live database. Retry on the
                # next bounded poll and let the browser retain its last snapshot.
                pass
            await asyncio.sleep(_POLL_INTERVAL)

    def _snapshot_marker(self, snapshot: dict[str, Any]) -> str:
        stable = {key: value for key, value in snapshot.items() if key != "refreshed_at"}
        signatures: list[tuple[str, int, int]] = []
        database = getattr(self.reader, "path", None)
        if database is not None:
            paths = (Path(database), Path(str(database) + "-wal"), Path(str(database) + "-shm"))
            for path in paths:
                try:
                    stat = path.stat()
                    signatures.append((str(path), stat.st_mtime_ns, stat.st_size))
                except OSError:
                    continue
        return hashlib.sha256(
            json.dumps([stable, signatures], sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def _publish(self, message: dict[str, Any]) -> None:
        for queue in tuple(self.clients):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                self.clients.discard(queue)
                while not queue.empty():
                    with suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                with suppress(asyncio.QueueFull):
                    queue.put_nowait(None)


def create_dashboard(root: Path, *, port: int = 8766, nonce: str = "") -> FastAPI:
    root = Path(root).resolve()
    dashboard_dir = root / "dashboard"
    reader = RuntimeReader(root)
    token = _token(dashboard_dir / "token")
    instance_generation = nonce or secrets.token_urlsafe(16)
    app_instance_id = secrets.token_urlsafe(16)
    poller = _Poller(reader)
    app = FastAPI(title="Incident Harness Dashboard", docs_url=None, redoc_url=None)
    app.state.port = port
    app.state.generation = instance_generation
    app.state.instance_id = app_instance_id
    app.state.reader = reader
    app.state.poller = poller
    app.state.sessions: dict[str, tuple[str, str | None, str, float]] = {}
    app.state.login_attempts: dict[str, list[float]] = {}

    def public_config() -> dict[str, Any]:
        try:
            value = json.loads((dashboard_dir / "public.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def public_contract(config: dict[str, Any]) -> tuple[bool, str]:
        hosts = config.get("hosts")
        secret = config.get("upstream_secret")
        generation = config.get("generation")
        scheme = config.get("scheme")
        try:
            public_port = int(config.get("public_port"))
        except (TypeError, ValueError):
            public_port = 0
        if not config.get("enabled"):
            return False, "public dashboard access is disabled"
        if (
            not isinstance(hosts, list)
            or not hosts
            or not all(isinstance(host, str) and host.strip() for host in hosts)
            or not isinstance(secret, str)
            or not secret
            or generation in (None, "")
            or scheme != "https"
            or not 1 <= public_port <= 65535
        ):
            return False, "invalid public dashboard routing configuration"
        return True, str(generation)

    def probe_contract(config: dict[str, Any]) -> tuple[bool, str]:
        """Validate the disabled-route HTTPS health probe contract."""
        hosts = config.get("hosts")
        secret = config.get("upstream_secret")
        probe_secret = config.get("probe_secret")
        generation = config.get("generation")
        try:
            public_port = int(config.get("public_port"))
        except (TypeError, ValueError):
            public_port = 0
        if (
            not isinstance(hosts, list)
            or not hosts
            or not all(isinstance(host, str) and host.strip() for host in hosts)
            or not isinstance(secret, str)
            or not secret
            or not isinstance(probe_secret, str)
            or not probe_secret
            or generation in (None, "")
            or config.get("scheme") != "https"
            or not 1 <= public_port <= 65535
        ):
            return False, "invalid public dashboard probe configuration"
        return True, str(generation)

    def public_hosts(config: dict[str, Any]) -> set[str]:
        try:
            public_port = int(config.get("public_port"))
        except (TypeError, ValueError):
            return set()
        if not isinstance(config.get("hosts"), list):
            return set()
        result = set()
        for value in config["hosts"]:
            if isinstance(value, str) and value.strip():
                host = value.strip().lower().rstrip(".")
                result.add(host if ":" in host else f"{host}:{public_port}")
        return result

    def classify(request: Request) -> tuple[_Access | None, str | None, int]:
        config = public_config()
        header_names = _header_names(request)
        host_header = request.headers.get("host", "").strip().lower().rstrip(".")
        configured_hosts = public_hosts(config)
        is_public_host = host_header in configured_hosts
        has_proxy_secret = "x-dashboard-upstream-secret" in header_names
        has_probe_secret = "x-dashboard-probe-secret" in header_names
        forwarded = header_names & _FORWARDED_NAMES
        unknown_proxy_header = any(
            name.startswith("x-forwarded-") and name != "x-forwarded-proto"
            for name in header_names
        ) or "x-dashboard-proxy-secret" in header_names
        valid_public, generation_or_error = public_contract(config)
        valid_probe, probe_generation_or_error = probe_contract(config)

        is_probe_request = request.method == "GET" and request.url.path == "/health"
        if has_probe_secret and not is_probe_request:
            return None, "dashboard probe credentials are valid only for GET /health", 400
        if is_probe_request and is_public_host and has_probe_secret:
            if not valid_probe:
                return None, probe_generation_or_error, 400
            expected_secret = str(config["upstream_secret"])
            expected_probe_secret = str(config["probe_secret"])
            supplied_secret = request.headers.get("x-dashboard-upstream-secret", "")
            supplied_probe_secret = request.headers.get("x-dashboard-probe-secret", "")
            if not hmac.compare_digest(
                supplied_secret.encode("utf-8"), expected_secret.encode("utf-8")
            ) or not hmac.compare_digest(
                supplied_probe_secret.encode("utf-8"), expected_probe_secret.encode("utf-8")
            ):
                return None, "invalid dashboard probe authentication", 400
            if request.headers.get("x-forwarded-proto", "") != "https":
                return None, "trusted dashboard proxy must set X-Forwarded-Proto: https", 400
            if any(
                name.startswith("x-forwarded-") and name != "x-forwarded-proto"
                for name in header_names
            ) or "forwarded" in header_names:
                return None, "unexpected forwarded headers", 400
            origin = request.headers.get("origin")
            if origin and not _origin_matches(origin, host_header, "https"):
                return None, "origin is not the configured dashboard host", 400
            return _Access("probe", probe_generation_or_error), None, 200
        if has_probe_secret:
            return None, "dashboard probe host is not configured", 400

        if is_public_host or has_proxy_secret or forwarded or unknown_proxy_header:
            if not valid_public:
                status = 403 if config.get("enabled") is False else 400
                return None, generation_or_error, status
            if not is_public_host:
                return None, "dashboard proxy host is not configured", 400
            expected_secret = str(config["upstream_secret"])
            supplied_secret = request.headers.get("x-dashboard-upstream-secret", "")
            if not hmac.compare_digest(
                supplied_secret.encode("utf-8"), expected_secret.encode("utf-8")
            ):
                return None, "invalid dashboard proxy authentication", 400
            if request.headers.get("x-forwarded-proto", "") != "https":
                return None, "trusted dashboard proxy must set X-Forwarded-Proto: https", 400
            if any(
                name.startswith("x-forwarded-") and name != "x-forwarded-proto"
                for name in header_names
            ) or "forwarded" in header_names:
                return None, "unexpected forwarded headers", 400
            origin = request.headers.get("origin")
            if origin and not _origin_matches(origin, host_header, "https"):
                return None, "origin is not the configured dashboard host", 400
            return _Access("public", generation_or_error), None, 200

        local_host = _host_without_port(host_header)
        if local_host not in _LOCAL_HOSTS:
            return None, "unrecognized dashboard host", 400
        try:
            parsed_host = urlsplit(f"//{host_header}")
            host_port = parsed_host.port
        except ValueError:
            return None, "malformed dashboard host", 400
        if host_port != app.state.port:
            return None, "dashboard host port does not match the listener", 400
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            if (
                parsed.scheme != request.url.scheme
                or parsed.netloc.lower().rstrip(".") != host_header
            ):
                return None, "origin is not the local dashboard host", 400
        return _Access("local"), None, 200

    @app.middleware("http")
    async def safety(request: Request, call_next):
        access, error, status = classify(request)
        if access is None:
            return JSONResponse(
                {"detail": error or "dashboard request rejected"}, status_code=status
            )
        request.state.dashboard_access = access
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self'; "
            "style-src 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    def _prune_sessions(now: float) -> None:
        for key, value in tuple(app.state.sessions.items()):
            if value[3] <= now:
                app.state.sessions.pop(key, None)

    def session_valid(request: Request) -> str:
        access: _Access = request.state.dashboard_access
        now = time.time()
        _prune_sessions(now)
        session_id = request.cookies.get(
            "dashboard_public_session" if access.scope == "public" else "dashboard_local_session"
        )
        item = app.state.sessions.get(session_id or "")
        if not item or item[0] != access.scope or item[2] != app_instance_id:
            raise HTTPException(401, "dashboard login required")
        if access.scope == "public":
            valid, current_generation = public_contract(public_config())
            if not valid or item[1] != current_generation:
                raise HTTPException(401, "public dashboard session revoked")
        if item[3] <= now:
            app.state.sessions.pop(session_id or "", None)
            raise HTTPException(401, "dashboard session expired")
        return session_id or ""

    async def bounded_body(request: Request) -> bytes:
        declared = request.headers.get("content-length")
        if declared:
            try:
                if int(declared) > _BODY_LIMIT:
                    raise HTTPException(413, "login body too large")
            except ValueError:
                pass
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > _BODY_LIMIT:
                raise HTTPException(413, "login body too large")
            chunks.append(chunk)
        return b"".join(chunks)

    def attempt_allowed(request: Request) -> bool:
        now = time.time()
        client = request.client.host if request.client else "unknown"
        access: _Access = request.state.dashboard_access
        key = f"{access.scope}:{client}"
        attempts = [
            value
            for value in app.state.login_attempts.get(key, [])
            if value > now - _LOGIN_WINDOW
        ]
        if len(attempts) >= _LOGIN_LIMIT:
            app.state.login_attempts[key] = attempts
            return False
        app.state.login_attempts[key] = attempts + [now]
        if len(app.state.login_attempts) > _LOGIN_KEY_LIMIT:
            oldest = min(
                app.state.login_attempts, key=lambda item: app.state.login_attempts[item][-1]
            )
            app.state.login_attempts.pop(oldest, None)
        return True

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await poller.start()
        try:
            yield
        finally:
            await poller.stop()

    app.router.lifespan_context = lifespan

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_asset("index.html"))

    @app.get("/health")
    async def health() -> dict[str, Any]:
        valid, generation_or_error = public_contract(public_config())
        return {
            "ok": True,
            "nonce": instance_generation,
            "public_enabled": valid,
            "public_generation": generation_or_error if valid else None,
        }

    @app.get("/assets/{name}")
    async def asset(name: str) -> Response:
        safe = Path(name).name
        if safe != name or safe not in {"index.html", "app.js", "style.css"}:
            raise HTTPException(404, "asset not found")
        content = _asset(safe)
        media = {"css": "text/css", "js": "text/javascript", "html": "text/html"}.get(
            Path(safe).suffix.lstrip("."), "text/plain"
        )
        return Response(content, media_type=media)

    @app.post("/login")
    async def login(request: Request):
        if not attempt_allowed(request):
            raise HTTPException(429, "too many login attempts")
        raw = await bounded_body(request)
        if request.headers.get("content-type", "").lower().startswith("application/json"):
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise HTTPException(422, "invalid login JSON") from None
            supplied = body.get("token", "") if isinstance(body, dict) else ""
        else:
            try:
                supplied = raw.decode("utf-8")
            except UnicodeDecodeError:
                supplied = ""
        if not isinstance(supplied, str) or token is None or not hmac.compare_digest(
            supplied.encode("utf-8"), token.encode("utf-8")
        ):
            raise HTTPException(401, "invalid dashboard token")
        access: _Access = request.state.dashboard_access
        session_id = secrets.token_urlsafe(32)
        now = time.time()
        _prune_sessions(now)
        while len(app.state.sessions) >= _SESSION_LIMIT:
            app.state.sessions.pop(next(iter(app.state.sessions)))
        app.state.sessions[session_id] = (
            access.scope,
            access.public_generation,
            app_instance_id,
            now + _SESSION_TTL,
        )
        response = JSONResponse({"ok": True})
        response.set_cookie(
            "dashboard_public_session" if access.scope == "public" else "dashboard_local_session",
            session_id,
            httponly=True,
            samesite="lax",
            secure=access.scope == "public",
            max_age=_SESSION_TTL,
            path="/",
        )
        return response

    @app.post("/logout")
    async def logout(request: Request):
        session_id = session_valid(request)
        app.state.sessions.pop(session_id, None)
        response = JSONResponse({"ok": True})
        response.delete_cookie("dashboard_local_session", path="/")
        response.delete_cookie("dashboard_public_session", path="/")
        return response

    @app.get("/api/snapshot")
    async def snapshot(request: Request):
        session_valid(request)
        query = request.query_params
        try:
            page = int(query.get("page", 1))
            page_size = int(query.get("page_size", 50))
            if page < 1 or page_size < 1 or page_size > 100:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(422, "page and page_size must be valid bounded integers") from None
        filters = {
            key: query.get(key, "")
            for key in ("application", "repository", "environment", "state", "since", "until")
        }
        for key in ("since", "until"):
            if filters[key]:
                try:
                    date.fromisoformat(filters[key])
                except ValueError:
                    raise HTTPException(422, f"{key} must be an ISO date") from None
        result = await asyncio.to_thread(
            reader.snapshot, page=page, page_size=page_size, filters=filters
        )
        return JSONResponse(result)

    @app.get("/api/tasks/{task_id}")
    async def task(task_id: str, request: Request):
        session_valid(request)
        item = await asyncio.to_thread(reader.task, task_id)
        if item is None:
            raise HTTPException(404, "task not found")
        return JSONResponse(item)

    @app.get("/api/events")
    async def events(request: Request):
        session_valid(request)
        await poller.start()
        queue = poller.subscribe()
        initial = await asyncio.to_thread(reader.snapshot)
        reconnect = bool(request.headers.get("last-event-id"))

        async def stream():
            try:
                initial_payload = dict(initial)
                initial_payload["reset"] = reconnect
                yield _sse("snapshot", initial_payload)
                while True:
                    if await request.is_disconnected() or not _session_still_valid(request):
                        break
                    try:
                        message = await asyncio.wait_for(queue.get(), timeout=_POLL_INTERVAL)
                    except TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    if message is None or not _session_still_valid(request):
                        break
                    payload = message.get("snapshot", message)
                    yield _sse("snapshot", payload)
            finally:
                poller.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _session_still_valid(request: Request) -> bool:
        try:
            session_valid(request)
        except HTTPException:
            return False
        return True

    return app


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'), default=str)}\n\n"


def _asset(name: str) -> str:
    path = Path(__file__).parent / "assets" / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        raise HTTPException(404, "asset not found") from None
