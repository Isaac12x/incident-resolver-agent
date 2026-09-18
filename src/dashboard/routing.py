"""Conservative, journaled publication of the dashboard through Caddy or nginx."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import http.client
import json
import os
import re
import secrets as _secrets
import shutil
import socket
import ssl
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class RoutingError(RuntimeError):
    """A publication precondition or proxy operation failed."""


COMMAND_TIMEOUT = 15.0
PROBE_TIMEOUT = 4.0
_SAFE_CONFIG_PATH = re.compile(r"^[A-Za-z0-9._/:-]+$")


def _sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (FileNotFoundError, OSError):
        return None


def _path_stat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _assert_regular(path: Path, *, label: str) -> os.stat_result:
    info = _path_stat(path)
    if info is None or stat.S_ISLNK(info.st_mode):
        raise RoutingError(f"{label} must be a regular, non-symlink file")
    if not stat.S_ISREG(info.st_mode):
        raise RoutingError(f"{label} must be a regular file")
    return info


def _atomic(
    path: Path, data: str, mode: int = 0o600, *, owner: tuple[int, int] | None = None
) -> None:
    """Atomically write a file while retaining its existing mode and owner."""
    existing = _path_stat(path)
    if existing is not None and stat.S_ISLNK(existing.st_mode):
        raise RoutingError(f"refusing to replace symlink {path}")
    if owner is None and existing is not None:
        owner = (existing.st_uid, existing.st_gid)
    if owner and hasattr(os, "geteuid") and os.geteuid() not in {0, owner[0]}:
        raise RoutingError(
            f"cannot preserve ownership of {path}; use its owner for routing changes"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, stat.S_IMODE(existing.st_mode) if existing else mode)
        if owner:
            try:
                os.fchown(fd, owner[0], owner[1])
            except OSError as exc:
                raise RoutingError(f"cannot preserve ownership of {path}: {exc}") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


@contextlib.contextmanager
def _lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {} if default is None else default
    return value if isinstance(value, dict) else {}


def _read_journal(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RoutingError(f"routing journal is malformed; recover {path} before retrying") from exc
    if not isinstance(value, dict):
        raise RoutingError(f"routing journal is malformed; recover {path} before retrying")
    return value


_BINDING_FIELDS = (
    "config",
    "config_owner",
    "proxy",
    "host",
    "port",
    "upstream_port",
    "snippet",
    "backup",
    "candidate",
    "original_config_hash",
    "installed_config_hash",
    "snippet_hash",
    "secret",
    "probe_secret",
)


def _binding_path(root: Path, state: dict[str, Any]) -> Path:
    config = Path(str(state.get("config", "")))
    if not config.is_absolute() or state.get("proxy") not in {"caddy", "nginx"}:
        raise RoutingError("routing journal has no canonical proxy binding")
    port = _port(state.get("port"), "saved public port")
    identity = hashlib.sha256(f"{root.resolve()}\0{config}".encode()).hexdigest()[:24]
    return config.parent / "incident-dashboard" / f"binding-{identity}-{port}.json"


def _assert_privileged_path(path: Path) -> None:
    """Require a root-controlled file and ancestry before trusting elevated recovery."""
    for current in (path, *path.parents):
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            raise RoutingError(
                f"privileged routing requires root-controlled files and directories: {current}; "
                "run without sudo for a user-owned proxy configuration"
            )


def _write_binding(root: Path, state: dict[str, Any]) -> None:
    """Keep immutable target identities outside the runtime user's writable directory."""
    config = Path(state["config"])
    if os.geteuid() == 0:
        _assert_privileged_path(config)
        _assert_privileged_path(config.parent / "incident-dashboard")
    info = _assert_regular(config, label="proxy configuration")
    payload = {key: state.get(key) for key in _BINDING_FIELDS}
    payload["runtime_root"] = str(root.resolve())
    _atomic(
        _binding_path(root, state),
        json.dumps(payload, sort_keys=True) + "\n",
        owner=(info.st_uid, info.st_gid),
    )


def _validate_binding(root: Path, state: dict[str, Any]) -> None:
    if os.geteuid() != 0 or not state or state.get("state") == "closed":
        return
    path = _binding_path(root, state)
    try:
        _assert_privileged_path(path)
        _assert_regular(path, label="protected routing binding")
        binding = _read_journal(path)
    except OSError as exc:
        raise RoutingError(
            "protected routing binding is missing; manual recovery required"
        ) from exc
    if binding.get("retired"):
        raise RoutingError("routing binding is retired; stale journal requires manual cleanup")
    if binding.get("runtime_root") != str(root.resolve()) or any(
        binding.get(key) != state.get(key) for key in _BINDING_FIELDS
    ):
        raise RoutingError("routing journal targets differ from the protected binding")
    if state.get("config_hash") not in {
        binding.get("original_config_hash"),
        binding.get("installed_config_hash"),
    }:
        raise RoutingError("routing journal configuration hash differs from the protected binding")
    config = Path(binding["config"])
    _assert_privileged_path(config)
    backup = Path(binding["backup"])
    if backup.exists() or backup.is_symlink():
        _assert_privileged_path(backup)
        _assert_regular(backup, label="protected routing backup")
        if _sha(backup) != binding["original_config_hash"]:
            raise RoutingError("routing backup differs from the protected original configuration")


def _retire_binding(root: Path, state: dict[str, Any]) -> None:
    """Revoke recovery authority before owned paths can be reused by another route."""
    path = _binding_path(root, state)
    if not path.exists():
        # A missing binding already confers no elevated recovery authority.
        return
    if os.geteuid() == 0:
        _assert_privileged_path(path)
    binding = _read_journal(path)
    binding["retired"] = True
    _atomic(path, json.dumps(binding, sort_keys=True) + "\n")


def _valid_host(host: str) -> bool:
    if len(host) > 253 or not host or host.startswith(".") or host.endswith("."):
        return False
    return bool(
        re.fullmatch(
            r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?",
            host,
        )
    )


def _port(value: Any, label: str = "port") -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RoutingError(f"invalid {label}: expected an integer from 1 through 65535") from exc
    if not 1 <= number <= 65535:
        raise RoutingError(f"invalid {label}: expected an integer from 1 through 65535")
    return number


@dataclass(frozen=True)
class Proxy:
    name: str
    executable: str
    config: Path


def _executable(name: str) -> str | None:
    fallback = {"caddy": "/opt/homebrew/bin/caddy", "nginx": "/opt/homebrew/bin/nginx"}.get(
        name, ""
    )
    return shutil.which(name) or (fallback if Path(fallback).exists() else None)


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command, text=True, capture_output=True, check=False, timeout=COMMAND_TIMEOUT
        )
    except subprocess.TimeoutExpired as exc:
        raise RoutingError(f"proxy command timed out after {COMMAND_TIMEOUT:g}s") from exc
    except OSError as exc:
        raise RoutingError(f"unable to run proxy command: {exc}") from exc


def _proxy_process_running(name: str) -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-x", name], text=True, capture_output=True, check=False, timeout=3
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _process_config(name: str) -> Path | None:
    try:
        result = subprocess.run(
            ["ps", "-axo", "command="], text=True, capture_output=True, check=False, timeout=3
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    pattern = re.compile(
        r"(?:^|[\s/])(?:"
        + re.escape(name)
        + r")(?:\s|$).*?(?:--config(?:uration)?\s+|\s-c\s+)([^\s]+)",
        re.I,
    )
    for line in result.stdout.splitlines():
        match = pattern.search(line)
        if match:
            path = Path(match.group(1).strip("\"'"))
            if path.is_file() and not path.is_symlink():
                return path
    return None


def _conventional_configs(root: Path, name: str) -> list[Path]:
    filename = "Caddyfile" if name == "caddy" else "nginx.conf"
    folders = "caddy" if name == "caddy" else "nginx"
    paths = [
        Path("/etc") / folders / filename,
        Path("/usr/local/etc") / folders / filename,
        Path("/opt/homebrew/etc") / folders / filename,
    ]
    local = root / "dashboard" / "proxy" / filename
    if local.exists():
        paths.append(local)
    return [path for path in paths if path.is_file() and not path.is_symlink()]


def _resolve_proxy(args: argparse.Namespace, root: Path) -> Proxy:
    selected = getattr(args, "proxy", None)
    if selected and selected not in {"caddy", "nginx"}:
        raise RoutingError(f"unsupported proxy {selected!r}; use caddy or nginx")
    explicit = getattr(args, "proxy_config", None) or getattr(args, "proxy_config_path", None)
    if explicit:
        config = Path(explicit).expanduser()
        if config.is_symlink():
            raise RoutingError(f"proxy configuration may not be a symlink: {config}")
        _assert_regular(config, label="proxy configuration")
        config = config.resolve()
        names = (
            [selected]
            if selected
            else (["caddy"] if config.name.lower() == "caddyfile" else ["nginx"])
        )
        if len(names) != 1:
            raise RoutingError("proxy selection is ambiguous; pass --proxy explicitly")
        return Proxy(names[0], _executable(names[0]) or names[0], config)
    names = [selected] if selected else ["caddy", "nginx"]
    candidates: list[Proxy] = []
    for name in names:
        executable = _executable(name)
        if not executable:
            continue
        config = _process_config(name)
        if config is None:
            conventional = _conventional_configs(root, name)
            if len(conventional) == 1 and _proxy_process_running(name):
                config = conventional[0]
        if config:
            candidates.append(Proxy(name, executable, config))
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise RoutingError("proxy detection is ambiguous; pass --proxy and --proxy-config")
    raise RoutingError(
        "no active supported proxy configuration found; pass --proxy and --proxy-config"
    )


def _listeners(text: str, proxy: str) -> set[int]:
    pattern = (
        r"(?m)^\s*[^\n{}#]*:(\d{1,5})\s*\{"
        if proxy == "caddy"
        else r"\blisten\s+(?:\[[^]]+\]:|[^;: ]+:)?(\d{1,5})\b"
    )
    return {int(item) for item in re.findall(pattern, text, re.I)}


def _occupied(port: int, host: str = "127.0.0.1") -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()


def _bind_occupied(port: int) -> bool:
    for family, address in ((socket.AF_INET, ("0.0.0.0", port)), (socket.AF_INET6, ("::", port))):
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    with contextlib.suppress(OSError):
                        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                sock.bind(address)
            finally:
                sock.close()
        except OSError:
            return True
    return False


def _safe_path(value: str | Path, label: str) -> str:
    text = str(value)
    if not _SAFE_CONFIG_PATH.fullmatch(text):
        raise RoutingError(f"{label} contains unsupported configuration characters")
    return text


def _snippet_directory(config: Path, info: os.stat_result) -> Path:
    """Return a proxy-readable directory for the generated secret-bearing include."""
    directory = config.parent / "incident-dashboard"
    existing = _path_stat(directory)
    if existing is not None and stat.S_ISLNK(existing.st_mode):
        raise RoutingError(f"refusing to use symlinked dashboard snippet directory {directory}")
    if existing is not None and not stat.S_ISDIR(existing.st_mode):
        raise RoutingError(f"dashboard snippet directory is not a directory: {directory}")
    if existing is None:
        try:
            directory.mkdir(mode=0o750)
        except OSError as exc:
            raise RoutingError(f"cannot create dashboard snippet directory: {exc}") from exc
    # Match the configuration's owner/group so the service which already reads
    # the proxy configuration can read the include after a process restart.
    directory_mode = (
        0o755 if info.st_mode & stat.S_IROTH else 0o750 if info.st_mode & stat.S_IRGRP else 0o700
    )
    try:
        os.chown(directory, info.st_uid, info.st_gid)
        os.chmod(directory, directory_mode)
    except OSError as exc:
        raise RoutingError(
            f"cannot prepare proxy-readable dashboard snippet directory: {exc}"
        ) from exc
    return directory


def _snippet_path(config: Path, info: os.stat_result, proxy: str, public: int) -> Path:
    return _snippet_directory(config, info) / f"{proxy}-{public}.conf"


def _candidate_path(config: Path, info: os.stat_result) -> Path:
    del info
    return config.parent / f".{config.name}.incident-dashboard-candidate"


def _backup_path(config: Path, info: os.stat_result, proxy: str, public: int) -> Path:
    return _snippet_directory(config, info) / f"{proxy}-{public}.config-before"


@contextlib.contextmanager
def _candidate(config: Path, info: os.stat_result, data: str):
    """Write and yield a same-directory candidate for validation before install."""
    path = _candidate_path(config, info)
    _atomic(path, data, stat.S_IMODE(info.st_mode), owner=(info.st_uid, info.st_gid))
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def _snippet(
    proxy: str,
    host: str,
    public: int,
    upstream: int,
    secret: str,
    cert: str | None,
    key: str | None,
) -> str:
    if (cert is None) != (key is None):
        raise RoutingError("TLS certificate and key must be supplied together")
    if cert:
        cert = _safe_path(cert, "TLS certificate path")
        key = _safe_path(key or "", "TLS key path")
    upstream_host = f"{host}:{public}"
    if proxy == "caddy":
        tls = f"    tls {cert} {key}\n" if cert and key else ""
        return (
            "# incident-harness dashboard\n"
            f"{host}:{public} {{\n"
            f"{tls}    reverse_proxy 127.0.0.1:{upstream} {{\n"
            f"        header_up X-Dashboard-Upstream-Secret {secret}\n"
            f"        header_up Host {upstream_host}\n"
            "        header_up X-Forwarded-Proto https\n"
            "        header_up -Forwarded\n"
            "        header_up -X-Forwarded-Host\n"
            "        header_up -X-Forwarded-For\n"
            "        flush_interval -1\n"
            "    }\n}\n"
        )
    if proxy != "nginx":
        raise RoutingError(f"unsupported proxy {proxy!r}")
    tls = f"    ssl_certificate {cert};\n    ssl_certificate_key {key};\n" if cert and key else ""
    return (
        "# incident-harness dashboard\n"
        "server {\n"
        f"    listen {public} ssl;\n    listen [::]:{public} ssl;\n"
        f"    server_name {host};\n{tls}"
        "    location / {\n"
        f"        proxy_set_header X-Dashboard-Upstream-Secret {secret};\n"
        f"        proxy_set_header Host {upstream_host};\n"
        "        proxy_set_header X-Forwarded-Proto https;\n"
        '        proxy_set_header Forwarded "";\n'
        '        proxy_set_header X-Forwarded-Host "";\n'
        '        proxy_set_header X-Forwarded-For "";\n'
        "        proxy_buffering off;\n        proxy_cache off;\n"
        "        proxy_http_version 1.1;\n        proxy_read_timeout 1h;\n"
        f"        proxy_pass http://127.0.0.1:{upstream};\n"
        "    }\n}\n"
    )


def _include(config: str, proxy: str, snippet: Path) -> str:
    marker = f"# incident-harness dashboard include: {snippet}"
    if marker in config:
        return config
    token = _safe_path(snippet, "dashboard snippet path")
    if proxy == "caddy":
        return config.rstrip() + f'\n{marker}\nimport "{token}"\n'
    start = re.search(r"(?m)^\s*http\s*\{", config)
    if not start:
        raise RoutingError("unsupported nginx layout: an explicit top-level http block is required")
    depth = 0
    end = None
    for index in range(start.end(), len(config)):
        if config[index] == "{":
            depth += 1
        elif config[index] == "}":
            if depth == 0:
                end = index
                break
            depth -= 1
    if end is None:
        raise RoutingError("unsupported nginx layout: unbalanced http block")
    return config[:end] + f'    {marker}\n    include "{token}";\n' + config[end:]


def _validate(proxy: Proxy, config: Path) -> None:
    command = (
        [proxy.executable, "validate", "--config", str(config), "--adapter", "caddyfile"]
        if proxy.name == "caddy"
        else [proxy.executable, "-t", "-p", str(config.parent) + "/", "-c", str(config)]
    )
    result = _run(command)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RoutingError(f"{proxy.name} configuration validation failed: {detail[:500]}")


def _reload(proxy: Proxy, config: Path) -> None:
    if proxy.name == "caddy":
        command = [proxy.executable, "reload"]
        admin = _caddy_admin(config)
        if admin:
            command.extend(["--address", admin])
        command.extend(["--config", str(config), "--adapter", "caddyfile"])
    else:
        command = [
            proxy.executable,
            "-p",
            str(config.parent) + "/",
            "-s",
            "reload",
            "-c",
            str(config),
        ]
    result = _run(command)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RoutingError(f"{proxy.name} graceful reload failed: {detail[:500]}")


def _caddy_admin(config: Path) -> str | None:
    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"(?m)^\s*admin\s+(\S+)", text)
    return match.group(1).strip('"') if match else None


def _probe(
    proxy: Proxy,
    host: str,
    port: int,
    secret: str,
    *,
    expect_dashboard: bool,
    probe_secret: str | None = None,
    expected_nonce: str | None = None,
) -> None:
    """Probe HTTPS with the intended Host and SNI before enabling the public generation."""
    context = ssl._create_unverified_context()
    deadline = time.monotonic() + PROBE_TIMEOUT
    while True:
        try:
            raw = socket.create_connection(("127.0.0.1", port), timeout=min(0.5, PROBE_TIMEOUT))
            with context.wrap_socket(raw, server_hostname=host) as stream:
                stream.settimeout(min(0.5, PROBE_TIMEOUT))
                probe_header = probe_secret or secret
                request = (
                    f"GET /health HTTP/1.1\r\nHost: {host}:{port}\r\n"
                    f"X-Dashboard-Upstream-Secret: {secret}\r\n"
                    f"X-Dashboard-Probe-Secret: {probe_header}\r\n"
                    "Connection: close\r\n\r\n"
                )
                stream.sendall(request.encode())
                response = http.client.HTTPResponse(stream)
                response.begin()
                body = response.read(4096).decode("utf-8", "replace")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {}
            is_dashboard = (
                response.status == 200 and isinstance(payload, dict) and payload.get("ok") is True
            )
            if not expect_dashboard and is_dashboard:
                if time.monotonic() >= deadline:
                    raise RoutingError("closed route still serves the dashboard")
                time.sleep(0.05)
                continue
            break
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            if not expect_dashboard or time.monotonic() >= deadline:
                if expect_dashboard:
                    raise RoutingError(f"local HTTPS dashboard probe failed: {exc}") from exc
                return
            time.sleep(0.05)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {}
    is_dashboard = (
        response.status == 200 and isinstance(payload, dict) and payload.get("ok") is True
    )
    if expected_nonce is not None:
        is_dashboard = is_dashboard and payload.get("nonce") == expected_nonce
    if expect_dashboard and not is_dashboard:
        raise RoutingError("local HTTPS probe did not reach the dashboard")
    if not expect_dashboard and is_dashboard:
        raise RoutingError("closed route still serves the dashboard")


def _state_paths(root: Path) -> tuple[Path, Path, Path]:
    directory = root / "dashboard"
    return directory / "public.json", directory / "routing.json", directory / "routing.lock"


def _dashboard_nonce(root: Path) -> str | None:
    value = _read_json(root / "dashboard" / "process.json")
    nonce = value.get("nonce")
    return nonce if isinstance(nonce, str) and nonce else None


def _atomic_dashboard(path: Path, data: str) -> None:
    """Write dashboard metadata with the dashboard directory's ownership."""
    existing = _path_stat(path)
    parent = _path_stat(path.parent)
    owner = (parent.st_uid, parent.st_gid) if existing is None and parent is not None else None
    _atomic(path, data, owner=owner)


def _revoke(public_path: Path, state: dict[str, Any]) -> None:
    previous = _read_json(public_path)
    value = dict(previous)
    value.update(
        {
            "enabled": False,
            "generation": int(previous.get("generation", state.get("generation", 0))) + 1,
            "updated_at": time.time(),
        }
    )
    _atomic_dashboard(public_path, json.dumps(value, sort_keys=True, indent=2) + "\n")


def _target(args: argparse.Namespace) -> tuple[str, int | None]:
    value = getattr(args, "target", None) or getattr(args, "port", None)
    if str(value).lower() == "close":
        return "close", None
    return "open", _port(value, "public port")


def add_port_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("target", help="public port to open, or close")
    parser.add_argument("--host")
    parser.add_argument("--proxy", choices=("caddy", "nginx"))
    parser.add_argument("--proxy-config", type=Path)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--upstream-port", type=int, default=8766)
    parser.add_argument("--route-path", default="/")
    return parser


def _remove_include(config: str, snippet: Path) -> str:
    marker = f"# incident-harness dashboard include: {snippet}"
    config = re.sub(r"(?m)^\s*" + re.escape(marker) + r"\n?", "", config)
    token = re.escape(str(snippet))
    return re.sub(
        r"(?m)^\s*(?:import\s+\"?" + token + r"\"?|include\s+\"?" + token + r"\"?;)\s*\n?",
        "",
        config,
    )


def _proxy_from_journal(state: dict[str, Any], args: argparse.Namespace, root: Path) -> Proxy:
    config = state.get("config")
    name = state.get("proxy")
    if not config or name not in {"caddy", "nginx"}:
        raise RoutingError("routing journal has no usable saved proxy configuration")
    path = Path(str(config))
    if not path.is_absolute():
        raise RoutingError("routing journal has a non-canonical proxy configuration path")
    info = _assert_regular(path, label="saved proxy configuration")
    owner = state.get("config_owner")
    if isinstance(owner, list) and len(owner) == 2 and (info.st_uid, info.st_gid) != tuple(owner):
        raise RoutingError("saved proxy configuration ownership changed; recovery files retained")
    saved = argparse.Namespace(**vars(args))
    saved.proxy = name
    saved.proxy_config = Path(config)
    return _resolve_proxy(saved, root)


def _recover_prepared(state_path: Path, public_path: Path, state: dict[str, Any]) -> None:
    _validate_binding(state_path.parent.parent, state)
    if state.get("state") not in {"prepared", "recovery_required"}:
        return
    if state.get("state") == "recovery_required" and state.get("operation") == "close":
        raise RoutingError(
            "routing cleanup requires manual recovery; public access remains revoked"
        )
    config = Path(str(state.get("config", "")))
    if not config.is_absolute():
        raise RoutingError("routing journal has a non-canonical proxy configuration path")
    info = _assert_regular(config, label="saved proxy configuration")
    owner = state.get("config_owner")
    if isinstance(owner, list) and len(owner) == 2 and (info.st_uid, info.st_gid) != tuple(owner):
        raise RoutingError("saved proxy configuration ownership changed; recovery files retained")
    backup = Path(str(state.get("backup", "")))
    proxy_name = state.get("proxy")
    try:
        public = _port(state.get("port"), "saved public port")
    except RoutingError:
        raise RoutingError("routing journal has no valid saved public port") from None
    expected_backup = _backup_path(config, info, str(proxy_name), public)
    if not backup.is_absolute() or backup.resolve() != expected_backup.resolve():
        raise RoutingError(
            "routing journal backup path is not the protected config-adjacent backup"
        )
    expected_snippet = _snippet_path(config, info, str(proxy_name), public)
    snippet_value = state.get("snippet")
    if snippet_value and Path(str(snippet_value)).resolve() != expected_snippet.resolve():
        raise RoutingError(
            "routing journal snippet path is not the protected config-adjacent include"
        )
    expected_candidate = _candidate_path(config, info)
    candidate_value = state.get("candidate")
    if candidate_value and Path(str(candidate_value)).resolve() != expected_candidate.resolve():
        raise RoutingError(
            "routing journal candidate path is not adjacent to the proxy configuration"
        )
    expected = state.get("installed_config_hash")
    current = _sha(config)
    original_hash = state.get("original_config_hash", state.get("config_hash"))
    if expected and current not in {expected, original_hash}:
        raise RoutingError(
            "interrupted routing operation found concurrent proxy edits; recovery files retained"
        )
    if backup.exists() or backup.is_symlink():
        backup_info = _assert_regular(backup, label="routing recovery backup")
        if backup_info.st_uid != info.st_uid or stat.S_IMODE(backup_info.st_mode) & 0o022:
            raise RoutingError("routing recovery backup ownership or permissions are not trusted")
        if _sha(backup) != original_hash:
            raise RoutingError("routing recovery backup does not match the original configuration")
    elif current != original_hash:
        raise RoutingError("routing recovery backup is missing; recovery files retained")
    if backup.is_file() and config.is_file() and current == expected:
        _atomic(
            config,
            backup.read_text(encoding="utf-8"),
            stat.S_IMODE(info.st_mode),
            owner=(info.st_uid, info.st_gid),
        )
    executable = _executable(str(proxy_name)) if proxy_name in {"caddy", "nginx"} else None
    if not executable:
        raise RoutingError("saved proxy executable is unavailable; recovery files retained")
    proxy = Proxy(str(proxy_name), executable, config)
    _validate(proxy, config)
    _reload(proxy, config)
    _retire_binding(state_path.parent.parent, state)
    backup.unlink(missing_ok=True)
    if state.get("snippet"):
        Path(str(state["snippet"])).unlink(missing_ok=True)
    if state.get("candidate"):
        Path(str(state["candidate"])).unlink(missing_ok=True)
    _revoke(public_path, state)
    state_path.unlink(missing_ok=True)


def _rollback(
    proxy: Proxy,
    original: str,
    *,
    installed_hash: str | None,
    mode: int,
    owner: tuple[int, int],
    backup: Path,
) -> None:
    original_hash = hashlib.sha256(original.encode()).hexdigest()
    if installed_hash and _sha(proxy.config) not in {installed_hash, original_hash}:
        raise RoutingError(
            "proxy configuration changed concurrently; rollback skipped and recovery "
            "backup retained"
        )
    _atomic(proxy.config, original, mode, owner=owner)
    try:
        _reload(proxy, proxy.config)
    except Exception as exc:
        raise RoutingError(
            "rollback restored the file but proxy reload failed; recovery backup retained "
            f"at {backup}: {exc}"
        ) from exc


def run_port_command(args: argparse.Namespace, root: Path) -> int:
    root = Path(root).expanduser().resolve()
    public_path, state_path, lock_path = _state_paths(root)
    action, public = _target(args)
    with _lock(lock_path):
        state = _read_journal(state_path)
        _validate_binding(root, state)
        if state.get("state") == "closed":
            state = {}
        if state.get("state") in {"prepared", "recovery_required"}:
            _recover_prepared(state_path, public_path, state)
            state = {}
        if action == "close":
            _revoke(public_path, state)
            if not state or state.get("state") in {"closed"}:
                print("Dashboard public route already closed")
                return 0
            try:
                proxy = _proxy_from_journal(state, args, root)
                snippet = Path(str(state["snippet"]))
                info = _assert_regular(proxy.config, label="proxy configuration")
                original = proxy.config.read_bytes().decode("utf-8")
                original_hash = hashlib.sha256(original.encode()).hexdigest()
                backup = Path(str(state.get("backup", "")))
                if backup.is_symlink():
                    raise RoutingError("routing recovery backup may not be a symlink")
                updated = (
                    backup.read_text(encoding="utf-8")
                    if backup.is_file() and _sha(proxy.config) == state.get("config_hash")
                    else _remove_include(original, snippet)
                )
                updated_hash = hashlib.sha256(updated.encode()).hexdigest()
                state.update(
                    {
                        "state": "closing",
                        "operation": "close",
                        "close_config_hash": _sha(proxy.config),
                    }
                )
                _atomic(state_path, json.dumps(state, sort_keys=True, indent=2) + "\n")
                if _sha(proxy.config) != original_hash:
                    raise RoutingError(
                        "proxy configuration changed concurrently; recovery files retained"
                    )
                installed = False
                try:
                    with _candidate(proxy.config, info, updated) as candidate_path:
                        _validate(proxy, candidate_path)
                        if _sha(proxy.config) != original_hash:
                            raise RoutingError(
                                "proxy configuration changed concurrently; recovery files retained"
                            )
                        installed = True
                        _atomic(
                            proxy.config,
                            updated,
                            stat.S_IMODE(info.st_mode),
                            owner=(info.st_uid, info.st_gid),
                        )
                    _reload(proxy, proxy.config)
                    _probe(
                        proxy,
                        str(state.get("host", "")),
                        _port(state.get("port")),
                        str(state.get("secret", "")),
                        expect_dashboard=False,
                        probe_secret=str(state.get("probe_secret", "")) or None,
                    )
                except Exception as exc:
                    if installed:
                        try:
                            _rollback(
                                proxy,
                                original,
                                installed_hash=updated_hash,
                                mode=stat.S_IMODE(info.st_mode),
                                owner=(info.st_uid, info.st_gid),
                                backup=backup,
                            )
                            state.update({"state": "close_failed", "error": str(exc)})
                            _atomic(state_path, json.dumps(state, sort_keys=True, indent=2) + "\n")
                        except Exception as rollback_exc:
                            state.update({"state": "recovery_required", "error": str(rollback_exc)})
                            _atomic(state_path, json.dumps(state, sort_keys=True, indent=2) + "\n")
                            raise RoutingError(
                                f"route cleanup failed; recovery required: {rollback_exc}"
                            ) from exc
                    else:
                        state.update({"state": "close_failed", "error": str(exc)})
                        _atomic(state_path, json.dumps(state, sort_keys=True, indent=2) + "\n")
                    raise RoutingError(
                        f"route cleanup failed; public access remains revoked: {exc}"
                    ) from exc
                _retire_binding(root, state)
                snippet.unlink(missing_ok=True)
                backup.unlink(missing_ok=True)
                _atomic(
                    state_path, json.dumps({"state": "closed", "enabled": False}, indent=2) + "\n"
                )
                print("Dashboard public route closed")
                return 0
            except (RoutingError, OSError, KeyError, ValueError) as exc:
                print(f"Public access revoked; routing cleanup incomplete: {exc}")
                return 1

        assert public is not None
        host = getattr(args, "host", None) or state.get("host")
        if not host or not _valid_host(host):
            raise RoutingError("--host is required and must be a valid hostname")
        upstream = _port(getattr(args, "upstream_port", 8766), "upstream port")
        if getattr(args, "route_path", "/") != "/":
            raise RoutingError("only route path / is supported")
        if state.get("state") == "active" and int(state.get("port", -1)) != public:
            raise RoutingError(
                "a different public route is already active; close it before changing ports"
            )
        if state.get("state") == "active":
            requested_host = getattr(args, "host", None)
            requested_proxy = getattr(args, "proxy", None)
            if requested_host and requested_host != state.get("host"):
                raise RoutingError(
                    "a different host is already active; close it before changing hosts"
                )
            if requested_proxy and requested_proxy != state.get("proxy"):
                raise RoutingError(
                    "a different proxy is already active; close it before changing proxies"
                )
        proxy = (
            _proxy_from_journal(state, args, root)
            if state.get("state") in {"active", "closing", "close_failed"}
            else _resolve_proxy(args, root)
        )
        if state.get("state") in {"closing", "close_failed"}:
            raise RoutingError("saved route cleanup is incomplete; retry 'dashboard port close'")
        info = _assert_regular(proxy.config, label="proxy configuration")
        if os.geteuid() == 0:
            _assert_privileged_path(proxy.config)
        original = proxy.config.read_bytes().decode("utf-8")
        original_hash = hashlib.sha256(original.encode()).hexdigest()
        mode = stat.S_IMODE(info.st_mode)
        owner = (info.st_uid, info.st_gid)
        configured = _listeners(original, proxy.name)
        if public in configured and not (
            state.get("state") == "active" and int(state.get("port", -1)) == public
        ):
            raise RoutingError(
                f"Conflict: TCP port {public} is already configured for another listener"
            )
        if state.get("state") != "active" and (
            _occupied(public, "127.0.0.1") or _occupied(public, "::1") or _bind_occupied(public)
        ):
            raise RoutingError(f"Conflict: TCP port {public} is already in use; no changes made.")
        cert_value = getattr(args, "tls_cert", None)
        key_value = getattr(args, "tls_key", None)
        cert = str(Path(cert_value).expanduser().resolve()) if cert_value else None
        key = str(Path(key_value).expanduser().resolve()) if key_value else None
        if (
            state.get("state") != "active"
            and proxy.name == "nginx"
            and not (cert and key and Path(cert).is_file() and Path(key).is_file())
        ):
            raise RoutingError(
                "nginx requires --tls-cert and --tls-key files (or an existing managed TLS layout)"
            )
        snippet = _snippet_path(proxy.config, info, proxy.name, public)
        candidate = _candidate_path(proxy.config, info)
        if state.get("state") == "active":
            if state.get("upstream_port") and int(state["upstream_port"]) != upstream:
                raise RoutingError(
                    "dashboard upstream changed; restart the dashboard before renewing the route"
                )
            if state.get("config_hash") != _sha(proxy.config) or state.get("snippet_hash") != _sha(
                snippet
            ):
                raise RoutingError(
                    "saved dashboard route changed concurrently; close it before republishing"
                )
            probe_value = str(state.get("probe_secret", "")) or str(state.get("secret", ""))
            _probe(
                proxy,
                host,
                public,
                str(state.get("secret", "")),
                expect_dashboard=True,
                probe_secret=probe_value,
                expected_nonce=_dashboard_nonce(root),
            )
            current_public = _read_json(public_path)
            if not current_public.get("enabled"):
                renewed = {
                    "enabled": True,
                    "generation": int(current_public.get("generation", 0)) + 1,
                    "upstream_secret": str(state.get("secret", "")),
                    "secret": str(state.get("secret", "")),
                    "probe_secret": str(state.get("probe_secret", "")),
                    "hosts": [host],
                    "host": host,
                    "public_port": public,
                    "port": public,
                    "scheme": "https",
                    "upstream_port": upstream,
                    "upstream": f"http://127.0.0.1:{upstream}",
                    "route_path": "/",
                    "proxy": proxy.name,
                    "tls": {"managed": proxy.name == "caddy"},
                    "updated_at": time.time(),
                }
                _atomic_dashboard(public_path, json.dumps(renewed, sort_keys=True, indent=2) + "\n")
                print(f"Dashboard route renewed locally at https://{host}:{public}/")
                return 0
            print(f"Dashboard route already configured locally at https://{host}:{public}/")
            return 0
        secret = str(state.get("secret") or _secrets.token_urlsafe(32))
        probe_secret = str(state.get("probe_secret") or _secrets.token_urlsafe(32))
        rendered = _snippet(proxy.name, host, public, upstream, secret, cert, key)
        updated = _include(original, proxy.name, snippet)
        backup = _backup_path(proxy.config, info, proxy.name, public)
        if snippet.exists() or snippet.is_symlink() or backup.exists() or backup.is_symlink():
            raise RoutingError(
                "Conflict: saved dashboard routing files already exist; recover their "
                "owning runtime before publishing"
            )
        record = {
            "proxy": proxy.name,
            "config": str(proxy.config),
            "config_owner": [owner[0], owner[1]],
            "config_hash": original_hash,
            "original_config_hash": original_hash,
            "installed_config_hash": hashlib.sha256(updated.encode()).hexdigest(),
            "snippet": str(snippet),
            "candidate": str(candidate),
            "snippet_hash": hashlib.sha256(rendered.encode()).hexdigest(),
            "backup": str(backup),
            "state": "prepared",
            "operation": "open",
            "host": host,
            "port": public,
            "upstream_port": upstream,
            "secret": secret,
            "probe_secret": probe_secret,
        }
        _write_binding(root, record)
        _atomic(state_path, json.dumps(record, sort_keys=True, indent=2) + "\n")
        _atomic(backup, original, 0o600, owner=owner)
        snippet_mode = (
            0o644
            if info.st_mode & stat.S_IROTH
            else 0o640
            if info.st_mode & stat.S_IRGRP
            else 0o600
        )
        _atomic(snippet, rendered, snippet_mode, owner=owner)
        if _sha(proxy.config) != original_hash:
            _revoke(public_path, record)
            raise RoutingError(
                "proxy configuration changed concurrently; no changes made (recovery "
                "journal retained)"
            )
        installed_hash: str | None = None
        try:
            with _candidate(proxy.config, info, updated) as candidate_path:
                _validate(proxy, candidate_path)
                if _sha(proxy.config) != original_hash:
                    raise RoutingError(
                        "proxy configuration changed concurrently; no changes made (recovery "
                        "journal retained)"
                    )
                installed_hash = record["installed_config_hash"]
                _atomic(proxy.config, updated, mode, owner=owner)
            if _sha(proxy.config) != installed_hash:
                raise RoutingError("proxy configuration changed during installation")
            record["installed_config_hash"] = installed_hash
            _atomic(state_path, json.dumps(record, sort_keys=True, indent=2) + "\n")
            _atomic_dashboard(
                public_path,
                json.dumps(
                    {
                        "enabled": False,
                        "generation": int(_read_json(public_path).get("generation", 0)),
                        "probe_secret": probe_secret,
                        "upstream_secret": secret,
                        "hosts": [host],
                        "public_port": public,
                        "scheme": "https",
                    },
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
            )
            _reload(proxy, proxy.config)
            _probe(
                proxy,
                host,
                public,
                secret,
                expect_dashboard=True,
                probe_secret=probe_secret,
                expected_nonce=_dashboard_nonce(root),
            )
            public_state = {
                "enabled": True,
                "generation": int(_read_json(public_path).get("generation", 0)) + 1,
                "upstream_secret": secret,
                "secret": secret,
                "probe_secret": probe_secret,
                "hosts": [host],
                "host": host,
                "public_port": public,
                "port": public,
                "scheme": "https",
                "upstream_port": upstream,
                "upstream": f"http://127.0.0.1:{upstream}",
                "route_path": "/",
                "proxy": proxy.name,
                "tls": {"managed": proxy.name == "caddy", "certificate": cert},
                "updated_at": time.time(),
            }
            _atomic_dashboard(
                public_path, json.dumps(public_state, sort_keys=True, indent=2) + "\n"
            )
            record.update(
                {
                    "state": "active",
                    "enabled": True,
                    "config_hash": installed_hash,
                    "snippet_hash": _sha(snippet),
                }
            )
            _atomic(state_path, json.dumps(record, sort_keys=True, indent=2) + "\n")
        except Exception as exc:
            if installed_hash is not None:
                try:
                    _rollback(
                        proxy,
                        original,
                        installed_hash=installed_hash,
                        mode=mode,
                        owner=owner,
                        backup=backup,
                    )
                except Exception as rollback_exc:
                    record.update({"state": "recovery_required", "error": str(rollback_exc)})
                    _atomic(state_path, json.dumps(record, sort_keys=True, indent=2) + "\n")
                    _revoke(public_path, record)
                    raise RoutingError(
                        f"publication failed; rollback requires recovery: {rollback_exc}"
                    ) from exc
            _retire_binding(root, record)
            snippet.unlink(missing_ok=True)
            backup.unlink(missing_ok=True)
            state_path.unlink(missing_ok=True)
            _revoke(public_path, record)
            raise RoutingError(f"publication failed and was rolled back: {exc}") from exc
        print(
            f"Dashboard route configured locally at https://{host}:{public}/ "
            "(internet reachability not verified)"
        )
        return 0


__all__ = ["RoutingError", "add_port_arguments", "run_port_command"]
