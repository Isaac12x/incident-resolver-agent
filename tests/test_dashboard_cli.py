import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.__main__ import parse_arguments
from src.dashboard import cli
from src.dashboard.cli import run_dashboard_command
from src.dashboard.process import DashboardError


def test_dashboard_parser_aliases_and_port_options():
    args = parse_arguments(
        [
            "--config",
            "config.toml",
            "dashboard",
            "--dettached",
            "--listen-port",
            "9000",
            "port",
            "8443",
        ]
    )
    assert args.detached is True
    assert args.listen_port == 9000
    assert args.action == "port"
    assert args.target == "8443"


def test_dashboard_close_parser():
    args = parse_arguments(["dashboard", "port", "close", "--proxy-config", "nginx.conf"])
    assert args.target == "close"
    assert args.proxy_config == Path("nginx.conf")


@pytest.mark.parametrize("exit_code", [0, 3])
def test_main_dispatches_dashboard_before_agent_startup(tmp_path, monkeypatch, exit_code):
    from src import __main__ as entry

    config = tmp_path / "missing-config.toml"
    calls = []

    def forbidden(*args, **kwargs):
        pytest.fail("dashboard must not bootstrap or construct the incident agent")

    def dashboard(args, path):
        calls.append((args.action, path))
        return exit_code

    monkeypatch.setattr(entry, "bootstrap", forbidden)
    monkeypatch.setattr(entry.Application, "build", forbidden)
    monkeypatch.setattr(entry, "run_dashboard_command", dashboard)
    command = ["--config", str(config), "dashboard", "status"]
    if exit_code:
        with pytest.raises(SystemExit) as error:
            entry.main(command)
        assert error.value.code == exit_code
    else:
        entry.main(command)
    assert calls == [("status", config)]
    assert not config.exists()


def test_invalid_listen_port_is_rejected():
    with pytest.raises(SystemExit):
        parse_arguments(["dashboard", "--listen-port", "70000"])
    with pytest.raises(SystemExit):
        parse_arguments(["dashboard", "--listen-port", "nope"])


def test_dashboard_rejects_invalid_action_shapes_without_loading_config(tmp_path: Path):
    common = {
        "action": "port",
        "target": None,
        "detached": False,
        "listen_port": 8766,
        "_internal": False,
        "_nonce": "",
    }
    with pytest.raises(SystemExit, match="requires PORT"):
        run_dashboard_command(SimpleNamespace(**common), tmp_path / "missing.toml")
    common["action"] = "status"
    common["target"] = "unexpected"
    with pytest.raises(SystemExit, match="does not accept"):
        run_dashboard_command(SimpleNamespace(**common), tmp_path / "missing.toml")


def test_internal_dashboard_requires_nonce(tmp_path: Path):
    args = SimpleNamespace(
        action=None,
        target=None,
        detached=False,
        listen_port=8766,
        _internal=True,
        _nonce="",
    )
    with pytest.raises(SystemExit, match="internal"):
        run_dashboard_command(args, tmp_path / "missing.toml")


def _args(action=None, target=None, *, detached=False, internal=False, nonce="n"):
    return SimpleNamespace(
        action=action,
        target=target,
        detached=detached,
        listen_port=8766,
        host=None,
        proxy=None,
        proxy_config=None,
        tls_cert=None,
        tls_key=None,
        route_path="/",
        _internal=internal,
        _nonce=nonce,
    )


def test_dashboard_dispatch_status_stop_and_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    root = tmp_path / "runtime"
    dashboard = root / "dashboard"
    dashboard.mkdir(parents=True)

    class FakeProcess:
        directory = dashboard

        def __init__(self, *args, **kwargs):
            self.stopped = False

        def existing(self):
            return {"pid": 42, "port": 8766, "nonce": "n", "detached": True}

        def wait_ready(self, state, timeout=2):
            return True

        def stop(self):
            self.stopped = True

        def logs(self):
            return "recent"

    monkeypatch.setattr(cli, "canonical_runtime_root", lambda path: root)
    monkeypatch.setattr(cli, "DashboardProcess", FakeProcess)
    dashboard.joinpath("public.json").write_text(
        '{"enabled":true,"host":"example.test","port":8443}', encoding="utf-8"
    )
    assert run_dashboard_command(_args("status"), tmp_path / "config.toml") == 0
    assert "public=https://example.test:8443" in capsys.readouterr().out
    assert run_dashboard_command(_args("logs"), tmp_path / "config.toml") == 0
    assert capsys.readouterr().out.strip() == "recent"
    assert run_dashboard_command(_args("stop"), tmp_path / "config.toml") == 0


def test_dashboard_dispatch_port_and_start_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    root = tmp_path / "runtime"
    root.joinpath("dashboard").mkdir(parents=True)

    class FakeProcess:
        directory = root / "dashboard"

        def __init__(self, *args, **kwargs):
            self.state = {"pid": 7, "port": 8766, "nonce": "n", "detached": True}

        def existing(self):
            return self.state

        def wait_ready(self, state, timeout=2):
            return True

        def start(self, detached=False):
            self.state["detached"] = detached
            return self.state

        def stop_foreground(self):
            self.cleaned = True

    monkeypatch.setattr(cli, "canonical_runtime_root", lambda path: root)
    monkeypatch.setattr(cli, "DashboardProcess", FakeProcess)
    import src.dashboard.routing as routing

    monkeypatch.setattr(routing, "run_port_command", lambda args, root: 0)
    args = _args("port", "8443")
    assert run_dashboard_command(args, tmp_path / "config.toml") == 0
    args = _args(None, detached=True)
    assert run_dashboard_command(args, tmp_path / "config.toml") == 0
    assert "already running" in capsys.readouterr().out


def test_dashboard_foreground_cleans_metadata_and_internal_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "runtime"
    root.joinpath("dashboard").mkdir(parents=True)

    class FakeProcess:
        directory = root / "dashboard"

        def __init__(self, *args, **kwargs):
            self.cleaned = False

        def existing(self):
            return None

        def start(self, detached=False):
            return {"pid": 1, "port": 8766, "nonce": "n", "detached": detached}

        def stop_foreground(self):
            self.cleaned = True

        def read_state(self):
            return {
                "pid": os.getpid(),
                "pid_start_identity": cli._start_identity(os.getpid()),
                "nonce": "n",
            }

    monkeypatch.setattr(cli, "canonical_runtime_root", lambda path: root)
    monkeypatch.setattr(cli, "DashboardProcess", FakeProcess)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    assert run_dashboard_command(_args(None), tmp_path / "config.toml") == 0
    assert run_dashboard_command(_args(None, internal=True), tmp_path / "config.toml") == 0


def test_dashboard_start_rejects_unready_duplicate_and_root_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "runtime"

    class FakeProcess:
        directory = root / "dashboard"

        def __init__(self, *args, **kwargs):
            pass

        def existing(self):
            return {"pid": 9, "port": 8766, "nonce": "n"}

        def wait_ready(self, state, timeout=2):
            return False

    monkeypatch.setattr(cli, "canonical_runtime_root", lambda path: root)
    monkeypatch.setattr(cli, "DashboardProcess", FakeProcess)
    with pytest.raises(SystemExit, match="not ready"):
        run_dashboard_command(_args(None), tmp_path / "config.toml")
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    with pytest.raises(SystemExit, match="refuses"):
        run_dashboard_command(_args(None), tmp_path / "config.toml")


def test_dashboard_dispatch_error_and_closed_port_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    root = tmp_path / "runtime"
    root.joinpath("dashboard").mkdir(parents=True)

    class FakeProcess:
        directory = root / "dashboard"

        def __init__(self, *args, **kwargs):
            pass

        def existing(self):
            return None

        def stop(self):
            raise OSError("cannot stop")

        def start(self, detached=False):
            raise DashboardError("cannot start")

    monkeypatch.setattr(cli, "canonical_runtime_root", lambda path: root)
    monkeypatch.setattr(cli, "DashboardProcess", FakeProcess)
    with pytest.raises(SystemExit, match="unknown dashboard action"):
        run_dashboard_command(_args("other"), tmp_path / "config.toml")
    with pytest.raises(SystemExit, match="cannot stop"):
        run_dashboard_command(_args("stop"), tmp_path / "config.toml")
    with pytest.raises(SystemExit, match="cannot start"):
        run_dashboard_command(_args(None), tmp_path / "config.toml")

    with pytest.raises(SystemExit, match="running detached"):
        run_dashboard_command(_args("port", "8443"), tmp_path / "config.toml")
    assert capsys.readouterr().out == ""


def test_dashboard_status_stopped_and_port_close_error_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    root = tmp_path / "runtime"
    root.joinpath("dashboard").mkdir(parents=True)

    class FakeProcess:
        directory = root / "dashboard"

        def __init__(self, *args, **kwargs):
            pass

        def existing(self):
            return None

        def wait_ready(self, state, timeout=2):
            return True

    monkeypatch.setattr(cli, "canonical_runtime_root", lambda path: root)
    monkeypatch.setattr(cli, "DashboardProcess", FakeProcess)
    assert run_dashboard_command(_args("status"), tmp_path / "config.toml") == 1
    assert "stopped" in capsys.readouterr().out
    import src.dashboard.routing as routing

    monkeypatch.setattr(routing, "run_port_command", lambda args, root: 0)
    assert run_dashboard_command(_args("port", "close"), tmp_path / "config.toml") == 0


def test_dashboard_rejects_internal_root_and_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    with pytest.raises(SystemExit, match="refuses"):
        run_dashboard_command(_args(None, internal=True), tmp_path / "config.toml")

    monkeypatch.setattr(cli.os, "geteuid", lambda: 501)
    root = tmp_path / "runtime"

    class FakeProcess:
        def __init__(self, *args, **kwargs):
            pass

        def read_state(self):
            return None

    monkeypatch.setattr(cli, "canonical_runtime_root", lambda path: root)
    monkeypatch.setattr(cli, "DashboardProcess", FakeProcess)
    clock = iter([0, 3])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(clock))
    with pytest.raises(SystemExit, match="identity"):
        run_dashboard_command(_args(None, internal=True), tmp_path / "config.toml")
