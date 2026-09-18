"""Safe lifecycle management for the dedicated dashboard process."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import signal
import socket
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from subprocess import Popen
from typing import Any

from ..config import load_config


class DashboardError(RuntimeError):
    """A user-actionable dashboard lifecycle error."""


def canonical_runtime_root(config_path: Path) -> Path:
    """Resolve the configured runtime without creating or migrating it."""
    config_path = config_path.expanduser().resolve()
    config = load_config(config_path, create=False)
    root = Path(config.runtime_root).expanduser()
    # Storage receives this path directly and therefore interprets relative
    # runtime roots against the invoking working directory.
    return (Path.cwd() / root if not root.is_absolute() else root).resolve()


def _start_identity(pid: int) -> str | None:
    """Return an OS process start identity suitable for PID reuse protection."""
    proc = Path(f"/proc/{pid}/stat")
    try:
        raw = proc.read_text(encoding="utf-8")
        fields = raw.rsplit(")", 1)[1].split()
        return fields[19]  # Linux kernel start time in clock ticks.
    except (OSError, IndexError):
        try:
            import subprocess

            result = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, check=False
            )
            return result.stdout.strip() or None
        except OSError:
            return None


def _alive(state: dict[str, Any]) -> bool:
    pid = state.get("pid")
    identity = state.get("pid_start_identity")
    if not isinstance(pid, int) or pid <= 0 or not identity:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # A zombie still responds to kill(pid, 0) and can retain the same /proc
    # start identity.  It is no longer a usable dashboard process.
    proc = Path(f"/proc/{pid}/stat")
    try:
        raw = proc.read_text(encoding="utf-8")
        if raw.rsplit(")", 1)[1].lstrip().startswith("Z "):
            return False
    except OSError:
        pass
    return _start_identity(pid) == identity


class DashboardProcess:
    def __init__(self, root: Path, *, port: int = 8766, config: Path | None = None) -> None:
        self.root = root.expanduser().resolve()
        try:
            self.port = int(port)
        except (TypeError, ValueError) as error:
            raise DashboardError("internal dashboard port must be an integer") from error
        if not 1 <= self.port <= 65535:
            raise DashboardError("internal dashboard port must be between 1 and 65535")
        self.config = config.resolve() if config else None
        self.directory = self.root / "dashboard"
        self.state_path = self.directory / "process.json"
        self.log_path = self.directory / "dashboard.log"
        # Process lifecycle and routing both mutate public.json.  One lock file
        # serializes those changes across the two modules.
        self.lock_path = self.directory / "routing.lock"

    def _prepare(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        if self.log_path.exists():
            os.chmod(self.log_path, 0o600)

    def read_state(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _write_state(self, state: dict[str, Any]) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.state_path)
        os.chmod(self.state_path, 0o600)

    @contextmanager
    def _startup_lock(self) -> Iterator[None]:
        self._prepare()
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as error:
            if "fd" in locals():
                os.close(fd)
            raise DashboardError("dashboard startup is already in progress") from error
        try:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def existing(self) -> dict[str, Any] | None:
        state = self.read_state()
        return state if state and _alive(state) else None

    def _assert_port_free(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", self.port))
        except OSError as error:
            raise DashboardError(
                f"internal dashboard port {self.port} is already in use"
            ) from error
        finally:
            sock.close()

    def start(self, *, detached: bool = False) -> dict[str, Any]:
        with self._startup_lock():
            current = self.existing()
            if current:
                if current.get("port") != self.port:
                    raise DashboardError(
                        f"dashboard is already running on port {current.get('port')}"
                    )
                if not self.wait_ready(current, timeout=2):
                    raise DashboardError("dashboard process exists but is not ready")
                return current
            self._revoke_public()
            self._assert_port_free()
            nonce = secrets.token_urlsafe(32)
            state = {
                "pid": os.getpid(),
                "pid_start_identity": _start_identity(os.getpid()),
                "port": self.port,
                "nonce": nonce,
                "log": str(self.log_path),
                "runtime_root": str(self.root),
                "started_at": time.time(),
                "detached": detached,
            }
            if detached:
                command = [
                    sys.executable,
                    "-m",
                    "src",
                    "--config",
                    str(self.config),
                    "dashboard",
                    "--_internal",
                    "--listen-port",
                    str(self.port),
                    f"--_nonce={nonce}",
                ]
                with self.log_path.open("a", encoding="utf-8") as log:
                    os.chmod(self.log_path, 0o600)
                    child = Popen(command, stdout=log, stderr=log, start_new_session=True)
                state["pid"] = child.pid
                state["pid_start_identity"] = None
                identity_deadline = time.monotonic() + 1
                while not state["pid_start_identity"] and time.monotonic() < identity_deadline:
                    time.sleep(0.01)
                    state["pid_start_identity"] = _start_identity(child.pid)
            self._write_state(state)
            if detached:
                if not self.wait_ready(state, timeout=15):
                    self._terminate(state)
                    self.state_path.unlink(missing_ok=True)
                    raise DashboardError("dashboard failed readiness; see dashboard logs")
                return state
            return state

    def stop_foreground(self) -> None:
        """Remove the current foreground instance's metadata after uvicorn exits."""
        with self._startup_lock():
            self._revoke_public()
            state = self.read_state()
            if state and state.get("pid") == os.getpid():
                self.state_path.unlink(missing_ok=True)

    def wait_ready(self, state: dict[str, Any], *, timeout: float = 15) -> bool:
        deadline = time.monotonic() + timeout
        url = f"http://127.0.0.1:{state.get('port', self.port)}/health"
        while time.monotonic() < deadline and _alive(state):
            try:
                with urllib.request.urlopen(url, timeout=0.5) as response:  # noqa: S310
                    payload = json.loads(response.read())
                    if payload.get("nonce") == state.get("nonce"):
                        return True
            except (OSError, ValueError, urllib.error.URLError):
                time.sleep(0.05)
        return False

    def _terminate(self, state: dict[str, Any]) -> None:
        if not _alive(state):
            return
        # A foreground supervisor records its own identity for status, but a
        # separate stop invocation must never signal that process accidentally.
        if state.get("pid") == os.getpid():
            return
        try:
            os.kill(state["pid"], signal.SIGTERM)
        except OSError:
            return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _alive(state):
            time.sleep(0.05)
        if _alive(state):
            try:
                os.kill(state["pid"], signal.SIGKILL)
            except OSError:
                return
            # Give SIGKILL a bounded opportunity to take effect before the final
            # identity check.  This matters for a stop caller racing process
            # teardown and avoids deleting metadata for a still-running PID.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _alive(state):
                time.sleep(0.05)

    def stop(self) -> bool:
        # Runtime metadata belongs to the runtime user. A privileged caller must
        # not turn a forged PID or lock-file symlink into authority over another
        # user's process or files.
        with self._runtime_owner():
            return self._stop_owned()

    @contextmanager
    def _runtime_owner(self) -> Iterator[None]:
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            yield
            return
        try:
            owner = self.directory.lstat()
        except FileNotFoundError:
            raise DashboardError("no dashboard runtime owner found") from None
        if not stat.S_ISDIR(owner.st_mode) or owner.st_uid == 0:
            raise DashboardError("dashboard stop requires a non-root runtime owner")
        original_gid, original_groups = os.getegid(), os.getgroups()
        try:
            os.setgroups([])
            os.setegid(owner.st_gid)
            os.seteuid(owner.st_uid)
            yield
        finally:
            os.seteuid(0)
            os.setegid(original_gid)
            os.setgroups(original_groups)

    def _stop_owned(self) -> bool:
        with self._startup_lock():
            state = self.read_state()
            self._revoke_public()
            if not state:
                return False
            self._terminate(state)
            if _alive(state):
                raise DashboardError("dashboard process did not stop")
            self.state_path.unlink(missing_ok=True)
            return True

    def _revoke_public(self) -> None:
        public = self.directory / "public.json"
        try:
            current = json.loads(public.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as error:
            raise DashboardError(f"could not read dashboard public state: {error}") from error
        if not isinstance(current, dict):
            raise DashboardError("dashboard public state is invalid")
        try:
            generation = int(current.get("generation", 0))
        except (TypeError, ValueError) as error:
            raise DashboardError("dashboard public generation is invalid") from error
        current.update({"enabled": False, "generation": generation + 1})
        # Write failures intentionally propagate.  Continuing with a stale
        # enabled route would expose a restarted dashboard.
        try:
            self._write_public(current)
        except OSError as error:
            raise DashboardError(f"could not revoke dashboard public access: {error}") from error

    def _write_public(self, value: dict[str, Any]) -> None:
        path = self.directory / "public.json"
        try:
            existing = path.lstat()
            if stat.S_ISLNK(existing.st_mode):
                raise DashboardError("dashboard public state must not be a symlink")
            owner = (existing.st_uid, existing.st_gid)
        except FileNotFoundError:
            owner = None
        fd, temporary_name = tempfile.mkstemp(prefix=".public.json.", dir=self.directory)
        try:
            os.fchmod(fd, 0o600)
            if owner is not None and hasattr(os, "fchown"):
                os.fchown(fd, *owner)
            handle = os.fdopen(fd, "w", encoding="utf-8")
            fd = -1
            with handle:
                handle.write(json.dumps(value, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        finally:
            if fd >= 0:
                os.close(fd)
            with suppress(FileNotFoundError):
                os.unlink(temporary_name)

    def logs(self, lines: int = 80) -> str:
        try:
            with self.log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - 64 * 1024))
                content = handle.read().decode("utf-8", errors="replace")
            return "\n".join(content.splitlines()[-lines:])
        except OSError:
            return ""
