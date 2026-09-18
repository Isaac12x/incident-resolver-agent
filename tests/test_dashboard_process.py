import json
import os
import socket
from pathlib import Path

import pytest

from src.dashboard.process import (
    DashboardError,
    DashboardProcess,
    _alive,
    _start_identity,
    canonical_runtime_root,
)


def test_state_is_private_and_logs_are_bounded(tmp_path: Path):
    process = DashboardProcess(tmp_path, port=8766)
    state = {
        "pid": os.getpid(),
        "pid_start_identity": "wrong",
        "port": 8766,
        "nonce": "n",
    }
    process._prepare()
    process._write_state(state)
    process.log_path.write_text("\n".join(str(i) for i in range(200)), encoding="utf-8")
    assert process.state_path.stat().st_mode & 0o777 == 0o600
    assert process.logs(3).splitlines() == ["197", "198", "199"]


def test_stale_pid_is_not_signalled(tmp_path: Path):
    process = DashboardProcess(tmp_path)
    process._prepare()
    process._write_state(
        {"pid": os.getpid(), "pid_start_identity": "stale", "port": 8766, "nonce": "n"}
    )
    assert process.existing() is None
    assert process.stop() is True
    assert not process.state_path.exists()


def test_busy_port_is_reported(tmp_path: Path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen()
    try:
        process = DashboardProcess(tmp_path, port=port)
        with pytest.raises(DashboardError, match="already in use"):
            process._assert_port_free()
    finally:
        sock.close()


def test_invalid_state_is_ignored(tmp_path: Path):
    process = DashboardProcess(tmp_path)
    process._prepare()
    process.state_path.write_text("not json", encoding="utf-8")
    assert process.read_state() is None


def test_public_generation_revoked_on_start(tmp_path: Path):
    process = DashboardProcess(tmp_path)
    process._prepare()
    process._write_public({"enabled": True, "generation": 4})
    process._revoke_public()
    payload = json.loads(process.directory.joinpath("public.json").read_text())
    assert payload == {"enabled": False, "generation": 5}


def test_startup_lock_rejects_reentrant_lock(tmp_path: Path):
    process = DashboardProcess(tmp_path)
    with process._startup_lock(), pytest.raises(DashboardError), process._startup_lock():
        pass


def test_log_file_is_private_and_invalid_ports_are_rejected(tmp_path: Path):
    process = DashboardProcess(tmp_path, port=12345)
    process._prepare()
    process.log_path.write_text("private\n", encoding="utf-8")
    process._prepare()
    assert process.log_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(DashboardError):
        DashboardProcess(tmp_path, port=0)
    with pytest.raises(DashboardError):
        DashboardProcess(tmp_path, port=65536)


def test_revoke_write_failure_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    process = DashboardProcess(tmp_path)
    process._prepare()
    process._write_public({"enabled": True, "generation": 1})
    monkeypatch.setattr(
        process, "_write_public", lambda value: (_ for _ in ()).throw(PermissionError("denied"))
    )
    with pytest.raises(DashboardError, match="revoke"):
        process._revoke_public()


def test_detached_instance_is_ready_and_stoppable(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(f"runtime_root = {str(tmp_path / 'runtime')!r}\n", encoding="utf-8")
    root = tmp_path / "runtime"
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    process = DashboardProcess(root, port=port, config=config)
    state = process.start(detached=True)
    try:
        assert state["detached"] is True
        assert process.wait_ready(state, timeout=2)
        assert process.log_path.stat().st_mode & 0o777 == 0o600
    finally:
        assert process.stop() is True
    assert process.read_state() is None


def test_process_identity_and_empty_state_paths(tmp_path: Path):
    process = DashboardProcess(tmp_path)
    assert not _alive({})
    assert process.logs() == ""
    assert process.stop() is False
    config = tmp_path / "config.toml"
    config.write_text(f"runtime_root = {str(tmp_path / 'runtime')!r}\n", encoding="utf-8")
    assert canonical_runtime_root(config) == (tmp_path / "runtime").resolve()
    assert _start_identity(os.getpid())


def test_start_returns_ready_existing_foreground_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    process = DashboardProcess(tmp_path, port=12345)
    process._prepare()
    state = {
        "pid": os.getpid(),
        "pid_start_identity": _start_identity(os.getpid()),
        "port": 12345,
        "nonce": "n",
    }
    process._write_state(state)
    monkeypatch.setattr(process, "wait_ready", lambda state, timeout=2: True)
    assert process.start() == state
    process._terminate(state)


def test_invalid_public_state_is_rejected(tmp_path: Path):
    process = DashboardProcess(tmp_path)
    process._prepare()
    process.directory.joinpath("public.json").write_text("[]", encoding="utf-8")
    with pytest.raises(DashboardError, match="invalid"):
        process._revoke_public()


def test_process_rejects_bad_public_generation_and_termination_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    process = DashboardProcess(tmp_path)
    process._prepare()
    process.directory.joinpath("public.json").write_text('{"generation":"bad"}', encoding="utf-8")
    with pytest.raises(DashboardError, match="generation"):
        process._revoke_public()
    monkeypatch.setattr("src.dashboard.process._alive", lambda state: True)
    monkeypatch.setattr(os, "kill", lambda *args: (_ for _ in ()).throw(OSError("gone")))
    process._terminate({"pid": 99999, "pid_start_identity": "x"})


def test_wait_ready_handles_offline_and_start_existing_wrong_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    process = DashboardProcess(tmp_path, port=12345)
    process._prepare()
    state = {
        "pid": os.getpid(),
        "pid_start_identity": _start_identity(os.getpid()),
        "port": 12345,
        "nonce": "expected",
    }
    monkeypatch.setattr(
        "src.dashboard.process.urllib.request.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(OSError("offline")),
    )
    assert process.wait_ready(state, timeout=0) is False
    process._write_state({**state, "port": 12346})
    monkeypatch.setattr(process, "wait_ready", lambda state, timeout=2: True)
    with pytest.raises(DashboardError, match="already running"):
        process.start()


def test_start_existing_not_ready_and_foreground_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    process = DashboardProcess(tmp_path, port=12345)
    process._prepare()
    state = {
        "pid": os.getpid(),
        "pid_start_identity": _start_identity(os.getpid()),
        "port": 12345,
        "nonce": "n",
    }
    process._write_state(state)
    monkeypatch.setattr(process, "wait_ready", lambda state, timeout=2: False)
    with pytest.raises(DashboardError, match="not ready"):
        process.start()
    process._write_state(state)
    process.stop_foreground()
    assert not process.state_path.exists()


def test_stop_reports_process_that_survives_termination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    process = DashboardProcess(tmp_path)
    process._prepare()
    state = {"pid": 54321, "pid_start_identity": "identity", "port": 8766, "nonce": "n"}
    process._write_state(state)
    monkeypatch.setattr(process, "_terminate", lambda state: None)
    monkeypatch.setattr("src.dashboard.process._alive", lambda state: True)
    with pytest.raises(DashboardError, match="did not stop"):
        process.stop()


def test_detached_readiness_failure_cleans_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = tmp_path / "config.toml"
    config.write_text(f"runtime_root = {str(tmp_path / 'runtime')!r}\n", encoding="utf-8")
    process = DashboardProcess(tmp_path / "runtime", port=12345, config=config)

    class Child:
        pid = 99999

    monkeypatch.setattr("src.dashboard.process.Popen", lambda *args, **kwargs: Child())
    monkeypatch.setattr("src.dashboard.process._start_identity", lambda pid: "identity")
    monkeypatch.setattr(process, "wait_ready", lambda state, timeout=15: False)
    with pytest.raises(DashboardError, match="readiness"):
        process.start(detached=True)
    assert not process.state_path.exists()


def test_stop_foreground_without_state_and_sigkill_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    process = DashboardProcess(tmp_path)
    process.stop_foreground()
    state = {"pid": 99999, "pid_start_identity": "identity"}
    alive_values = iter([True, True, False, True, False])
    monkeypatch.setattr("src.dashboard.process._alive", lambda value: next(alive_values))
    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, signal: sent.append(signal))
    monkeypatch.setattr("src.dashboard.process.time.monotonic", lambda: 0)
    monkeypatch.setattr("src.dashboard.process.time.sleep", lambda value: None)
    process._terminate(state)
    assert sent == [15, 9]


def test_public_rewrite_preserves_existing_owner(tmp_path: Path):
    process = DashboardProcess(tmp_path)
    process._prepare()
    process._write_public({"enabled": True, "generation": 1})
    original = process.directory.joinpath("public.json").stat()
    process._revoke_public()
    rewritten = process.directory.joinpath("public.json").stat()
    assert (rewritten.st_uid, rewritten.st_gid) == (original.st_uid, original.st_gid)
    assert rewritten.st_mode & 0o777 == original.st_mode & 0o777


def test_public_write_rejects_symlink_and_ignores_planted_temp_symlink(tmp_path: Path):
    process = DashboardProcess(tmp_path)
    process._prepare()
    victim = tmp_path / "victim"
    victim.write_text("untouched", encoding="utf-8")
    process.directory.joinpath("public.tmp").symlink_to(victim)
    process._write_public({"enabled": False, "generation": 1})
    assert victim.read_text(encoding="utf-8") == "untouched"
    public = process.directory / "public.json"
    public.unlink()
    public.symlink_to(victim)
    with pytest.raises(DashboardError, match="symlink"):
        process._write_public({"enabled": False, "generation": 2})
    assert victim.read_text(encoding="utf-8") == "untouched"


def test_privileged_stop_uses_runtime_owner_and_restores_identity(tmp_path, monkeypatch):
    process = DashboardProcess(tmp_path)
    process.directory.mkdir()
    owner = process.directory.stat()
    identity = {"uid": 0, "gid": 0, "groups": [0, 42]}
    monkeypatch.setattr(os, "geteuid", lambda: identity["uid"])
    monkeypatch.setattr(os, "getegid", lambda: identity["gid"])
    monkeypatch.setattr(os, "getgroups", lambda: identity["groups"])
    monkeypatch.setattr(os, "seteuid", lambda value: identity.update(uid=value))
    monkeypatch.setattr(os, "setegid", lambda value: identity.update(gid=value))
    monkeypatch.setattr(os, "setgroups", lambda value: identity.update(groups=value))

    def stop_owned():
        assert identity == {"uid": owner.st_uid, "gid": owner.st_gid, "groups": []}
        raise DashboardError("revocation failed")

    monkeypatch.setattr(process, "_stop_owned", stop_owned)
    with pytest.raises(DashboardError, match="revocation failed"):
        process.stop()
    assert identity == {"uid": 0, "gid": 0, "groups": [0, 42]}


def test_privileged_stop_rejects_missing_or_symlinked_runtime_owner(tmp_path, monkeypatch):
    process = DashboardProcess(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(DashboardError, match="no dashboard runtime owner"):
        process.stop()
    target = tmp_path / "other"
    target.mkdir()
    process.directory.symlink_to(target, target_is_directory=True)
    with pytest.raises(DashboardError, match="non-root runtime owner"):
        process.stop()
