from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from src.dashboard import routing


@pytest.fixture(autouse=True)
def fake_https_probe(monkeypatch, request):
    if request.node.name in {
        "test_additional_transaction_and_probe_edges",
        "test_binding_path_and_probe_failure_branches",
    }:
        return
    monkeypatch.setattr(routing, "_probe", lambda *args, **kwargs: None)


def ns(**values):
    defaults = dict(
        target="8443",
        host="incidents.example.test",
        proxy="caddy",
        proxy_config=None,
        tls_cert=None,
        tls_key=None,
        upstream_port=8766,
        route_path="/",
    )
    defaults.update(values)
    return argparse.Namespace(**defaults)


def test_new_publication_discards_untrusted_closed_journal_fields(tmp_path, monkeypatch):
    config = tmp_path / "Caddyfile"
    config.write_text(":9000 { respond ok }\n", encoding="utf-8")
    dashboard = tmp_path / "dashboard"
    dashboard.mkdir()
    injected = "unsafe\nconfiguration directive"
    (dashboard / "routing.json").write_text(
        json.dumps({"state": "closed", "secret": injected, "probe_secret": injected}),
        encoding="utf-8",
    )
    monkeypatch.setattr(routing, "_executable", lambda name: "/fake/caddy")
    monkeypatch.setattr(routing, "_occupied", lambda port, host: False)
    monkeypatch.setattr(routing, "_bind_occupied", lambda port: False)
    monkeypatch.setattr(
        routing, "_run", lambda command: subprocess.CompletedProcess(command, 0, "", "")
    )
    assert routing.run_port_command(ns(proxy_config=config), tmp_path) == 0
    state = json.loads((dashboard / "routing.json").read_text())
    assert state["secret"] != injected
    assert state["probe_secret"] != injected
    assert injected not in Path(state["snippet"]).read_text()


def test_caddy_open_close_writes_contract_and_removes_owned_route(tmp_path, monkeypatch, capsys):
    config = tmp_path / "Caddyfile"
    config.write_text(":9000 { respond ok }\n", encoding="utf-8")
    monkeypatch.setattr(routing, "_executable", lambda name: "/fake/caddy")
    monkeypatch.setattr(routing, "_occupied", lambda port, host: False)
    monkeypatch.setattr(routing, "_bind_occupied", lambda port: False)
    monkeypatch.setattr(
        routing,
        "_run",
        lambda command: routing.subprocess.CompletedProcess(command, 0, "", ""),
    )

    assert routing.run_port_command(ns(proxy_config=config), tmp_path) == 0
    public = json.loads((tmp_path / "dashboard/public.json").read_text())
    assert public["enabled"] is True
    assert public["generation"] == 1
    assert public["secret"] == public["upstream_secret"]
    snippet = next(config.parent.joinpath("incident-dashboard").glob("*.conf"))
    assert "X-Dashboard-Upstream-Secret" in snippet.read_text()
    assert "import" in config.read_text()
    assert routing.run_port_command(ns(proxy_config=config), tmp_path) == 0
    assert json.loads((tmp_path / "dashboard/public.json").read_text())["generation"] == 1

    assert (
        routing.run_port_command(ns(target="close", proxy=None, proxy_config=None), tmp_path) == 0
    )
    public = json.loads((tmp_path / "dashboard/public.json").read_text())
    assert public["enabled"] is False
    assert "import" not in config.read_text()
    assert not snippet.exists()
    assert "internet reachability" in capsys.readouterr().out


def test_rejects_occupied_listener_without_mutating_config(tmp_path, monkeypatch):
    config = tmp_path / "Caddyfile"
    original = "incidents.example.test:8443 { respond ok }\n"
    config.write_text(original, encoding="utf-8")
    monkeypatch.setattr(routing, "_executable", lambda name: "/fake/caddy")
    monkeypatch.setattr(routing, "_occupied", lambda port, host: False)
    monkeypatch.setattr(routing, "_bind_occupied", lambda port: False)
    with pytest.raises(routing.RoutingError, match="already configured"):
        routing.run_port_command(ns(proxy_config=config), tmp_path)
    assert config.read_text() == original


def test_rejects_second_route_until_first_is_closed(tmp_path, monkeypatch):
    config = tmp_path / "Caddyfile"
    config.write_text(":9000 { respond ok }\n", encoding="utf-8")
    monkeypatch.setattr(routing, "_executable", lambda name: "/fake/caddy")
    monkeypatch.setattr(routing, "_occupied", lambda port, host: False)
    monkeypatch.setattr(routing, "_bind_occupied", lambda port: False)
    monkeypatch.setattr(
        routing,
        "_run",
        lambda command: routing.subprocess.CompletedProcess(command, 0, "", ""),
    )
    assert routing.run_port_command(ns(proxy_config=config), tmp_path) == 0
    with pytest.raises(routing.RoutingError, match="close it"):
        routing.run_port_command(ns(target="9443", proxy_config=config), tmp_path)


def test_nginx_requires_explicit_tls_files(tmp_path, monkeypatch):
    config = tmp_path / "nginx.conf"
    config.write_text("events {}\nhttp { include mime.types; }\n", encoding="utf-8")
    monkeypatch.setattr(routing, "_executable", lambda name: "/fake/nginx")
    monkeypatch.setattr(routing, "_occupied", lambda port, host: False)
    monkeypatch.setattr(routing, "_bind_occupied", lambda port: False)
    with pytest.raises(routing.RoutingError, match="requires --tls-cert"):
        routing.run_port_command(ns(proxy="nginx", proxy_config=config), tmp_path)


def test_saved_route_config_is_authoritative_for_idempotent_open(tmp_path, monkeypatch):
    first = tmp_path / "first.Caddyfile"
    second = tmp_path / "second.Caddyfile"
    first.write_text(":9000 { respond ok }\n", encoding="utf-8")
    second.write_text(":9001 { respond unrelated }\n", encoding="utf-8")
    monkeypatch.setattr(routing, "_executable", lambda name: "/fake/caddy")
    monkeypatch.setattr(routing, "_occupied", lambda port, host: False)
    monkeypatch.setattr(routing, "_bind_occupied", lambda port: False)
    monkeypatch.setattr(
        routing,
        "_run",
        lambda command: routing.subprocess.CompletedProcess(command, 0, "", ""),
    )
    assert routing.run_port_command(ns(proxy_config=first), tmp_path) == 0
    assert routing.run_port_command(ns(proxy_config=second), tmp_path) == 0
    assert "incident-harness" in first.read_text(encoding="utf-8")
    assert "incident-harness" not in second.read_text(encoding="utf-8")


def test_disabled_saved_route_is_renewed_after_dashboard_restart(tmp_path, monkeypatch):
    config = tmp_path / "Caddyfile"
    config.write_text(":9000 { respond ok }\n", encoding="utf-8")
    monkeypatch.setattr(routing, "_executable", lambda name: "/fake/caddy")
    monkeypatch.setattr(routing, "_occupied", lambda port, host: False)
    monkeypatch.setattr(routing, "_bind_occupied", lambda port: False)
    monkeypatch.setattr(
        routing, "_run", lambda command: subprocess.CompletedProcess(command, 0, "", "")
    )
    args = ns(proxy_config=config)
    assert routing.run_port_command(args, tmp_path) == 0
    public_path = tmp_path / "dashboard/public.json"
    public = json.loads(public_path.read_text())
    public["enabled"] = False
    public_path.write_text(json.dumps(public), encoding="utf-8")
    assert routing.run_port_command(args, tmp_path) == 0
    renewed = json.loads(public_path.read_text())
    assert renewed["enabled"] is True
    assert renewed["generation"] == public["generation"] + 1


def test_malformed_journal_is_rejected_without_touching_proxy(tmp_path):
    config = tmp_path / "Caddyfile"
    config.write_text(":9000 { respond ok }\n", encoding="utf-8")
    journal = tmp_path / "dashboard" / "routing.json"
    journal.parent.mkdir()
    journal.write_text("{ definitely not json", encoding="utf-8")
    with pytest.raises(routing.RoutingError, match="journal is malformed"):
        routing.run_port_command(ns(proxy_config=config), tmp_path)
    assert config.read_text(encoding="utf-8") == ":9000 { respond ok }\n"


def test_symlinked_proxy_config_is_refused(tmp_path, monkeypatch):
    real = tmp_path / "real.Caddyfile"
    link = tmp_path / "Caddyfile"
    real.write_text(":9000 { respond ok }\n", encoding="utf-8")
    link.symlink_to(real)
    monkeypatch.setattr(routing, "_executable", lambda name: "/fake/caddy")
    with pytest.raises(routing.RoutingError, match="may not be a symlink"):
        routing.run_port_command(ns(proxy_config=link), tmp_path)


def test_rollback_failure_retains_recovery_journal_and_revokes_public_access(tmp_path, monkeypatch):
    config = tmp_path / "Caddyfile"
    config.write_text(":9000 { respond ok }\n", encoding="utf-8")
    monkeypatch.setattr(routing, "_executable", lambda name: "/fake/caddy")
    monkeypatch.setattr(routing, "_occupied", lambda port, host: False)
    monkeypatch.setattr(routing, "_bind_occupied", lambda port: False)
    monkeypatch.setattr(
        routing,
        "_run",
        lambda command: routing.subprocess.CompletedProcess(command, 0, "", ""),
    )
    calls = {"reload": 0}

    def failed_reload(proxy, path):
        calls["reload"] += 1
        raise routing.RoutingError("reload unavailable")

    monkeypatch.setattr(routing, "_reload", failed_reload)
    with pytest.raises(routing.RoutingError, match="recovery"):
        routing.run_port_command(ns(proxy_config=config), tmp_path)
    journal = json.loads((tmp_path / "dashboard" / "routing.json").read_text())
    assert journal["state"] == "recovery_required"
    assert Path(journal["backup"]).is_file()
    public = json.loads((tmp_path / "dashboard" / "public.json").read_text())
    assert public["enabled"] is False
    assert calls["reload"] >= 2


def test_rendering_rejects_unsafe_paths_and_incomplete_tls_pair():
    with pytest.raises(routing.RoutingError, match="unsupported configuration"):
        routing._snippet("caddy", "example.test", 8443, 8766, "s", "/tmp/a b", "/tmp/key")
    with pytest.raises(routing.RoutingError, match="supplied together"):
        routing._snippet("caddy", "example.test", 8443, 8766, "s", "/tmp/cert", None)


def test_config_helpers_cover_supported_and_unsupported_layouts(tmp_path):
    assert routing._valid_host("incidents.example.test")
    assert not routing._valid_host("-bad.example.test")
    assert routing._port("8443") == 8443
    with pytest.raises(routing.RoutingError):
        routing._port("0")
    assert routing._listeners("example.test:443 { respond ok }", "caddy") == {443}
    assert routing._listeners("events {}\nhttp { listen 443 ssl; }", "nginx") == {443}
    with pytest.raises(routing.RoutingError, match="top-level http"):
        routing._include("events {}", "nginx", tmp_path / "snippet.conf")
    assert "include" in routing._include("events {}\nhttp { }", "nginx", tmp_path / "snippet.conf")
    caddy = routing._include(":9000 { respond ok }", "caddy", tmp_path / "snippet.conf")
    assert 'import "' in caddy
    assert str(tmp_path / "snippet.conf") not in routing._remove_include(
        caddy, tmp_path / "snippet.conf"
    )


def test_active_proxy_detection_requires_running_process_or_explicit_override(
    tmp_path, monkeypatch
):
    config = tmp_path / "Caddyfile"
    config.write_text(":9000 { respond ok }\n", encoding="utf-8")
    monkeypatch.setattr(routing, "_executable", lambda name: "/fake/" + name)
    monkeypatch.setattr(routing, "_process_config", lambda name: None)
    monkeypatch.setattr(routing, "_conventional_configs", lambda root, name: [config])
    monkeypatch.setattr(routing, "_proxy_process_running", lambda name: False)
    with pytest.raises(routing.RoutingError, match="no active"):
        routing._resolve_proxy(argparse.Namespace(proxy=None, proxy_config=None), tmp_path)
    proxy = routing._resolve_proxy(argparse.Namespace(proxy="caddy", proxy_config=config), tmp_path)
    assert proxy.config == config


def test_proxy_detection_rejects_ambiguity(tmp_path, monkeypatch):
    configs = {"caddy": tmp_path / "Caddyfile", "nginx": tmp_path / "nginx.conf"}
    for path in configs.values():
        path.write_text("ok\n", encoding="utf-8")
    monkeypatch.setattr(routing, "_executable", lambda name: "/fake/" + name)
    monkeypatch.setattr(routing, "_process_config", lambda name: configs[name])
    with pytest.raises(routing.RoutingError, match="ambiguous"):
        routing._resolve_proxy(argparse.Namespace(proxy=None, proxy_config=None), tmp_path)


def test_open_validation_failure_rolls_back_and_revokes(tmp_path, monkeypatch):
    config = tmp_path / "Caddyfile"
    original = ":9000 { respond ok }\n"
    config.write_text(original, encoding="utf-8")
    monkeypatch.setattr(routing, "_executable", lambda name: "/fake/caddy")
    monkeypatch.setattr(routing, "_occupied", lambda port, host: False)
    monkeypatch.setattr(routing, "_bind_occupied", lambda port: False)
    monkeypatch.setattr(
        routing,
        "_validate",
        lambda proxy, path: (_ for _ in ()).throw(routing.RoutingError("invalid")),
    )
    monkeypatch.setattr(routing, "_reload", lambda proxy, path: None)
    with pytest.raises(routing.RoutingError, match="rolled back"):
        routing.run_port_command(ns(proxy_config=config), tmp_path)
    assert config.read_text(encoding="utf-8") == original
    assert json.loads((tmp_path / "dashboard/public.json").read_text())["enabled"] is False


def test_nginx_open_and_close_preserve_config_mode(tmp_path, monkeypatch):
    config = tmp_path / "nginx.conf"
    config.write_text("events {}\nhttp { }\n", encoding="utf-8")
    config.chmod(0o640)
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert.write_text("cert")
    key.write_text("key")
    monkeypatch.setattr(routing, "_executable", lambda name: "/fake/nginx")
    monkeypatch.setattr(routing, "_occupied", lambda port, host: False)
    monkeypatch.setattr(routing, "_bind_occupied", lambda port: False)
    monkeypatch.setattr(
        routing, "_run", lambda command: subprocess.CompletedProcess(command, 0, "", "")
    )
    args = ns(proxy="nginx", proxy_config=config, tls_cert=cert, tls_key=key)
    assert routing.run_port_command(args, tmp_path) == 0
    assert config.stat().st_mode & 0o777 == 0o640
    args.target = "close"
    assert routing.run_port_command(args, tmp_path) == 0
    assert config.read_text(encoding="utf-8") == "events {}\nhttp { }\n"


def test_low_level_file_and_json_guards(tmp_path):
    missing = tmp_path / "missing"
    assert routing._sha(missing) is None
    assert routing._path_stat(missing) is None
    assert routing._read_json(missing) == {}
    malformed = tmp_path / "bad.json"
    malformed.write_text("[1]", encoding="utf-8")
    assert routing._read_json(malformed) == {}
    journal = tmp_path / "routing.json"
    journal.write_text("not-json", encoding="utf-8")
    with pytest.raises(routing.RoutingError, match="journal is malformed"):
        routing._read_journal(journal)
    directory = tmp_path / "dir"
    directory.mkdir()
    with pytest.raises(routing.RoutingError, match="regular"):
        routing._assert_regular(directory, label="config")
    symlink = tmp_path / "link"
    symlink.symlink_to(directory)
    with pytest.raises(routing.RoutingError, match="regular"):
        routing._assert_regular(symlink, label="config")
    with pytest.raises(routing.RoutingError, match="unsupported"):
        routing._safe_path("/tmp/unsafe path", "path")


def test_atomic_refuses_symlink_and_preserves_existing_metadata(tmp_path):
    target = tmp_path / "target"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o640)
    routing._atomic(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert target.stat().st_mode & 0o777 == 0o640
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(routing.RoutingError, match="symlink"):
        routing._atomic(link, "bad")


def test_run_timeout_and_proxy_command_errors(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1)

    monkeypatch.setattr(routing.subprocess, "run", timeout)
    with pytest.raises(routing.RoutingError, match="timed out"):
        routing._run(["proxy"])

    def missing(*args, **kwargs):
        raise OSError("missing")

    monkeypatch.setattr(routing.subprocess, "run", missing)
    with pytest.raises(routing.RoutingError, match="unable to run"):
        routing._run(["proxy"])


def test_process_config_and_conventional_detection(tmp_path, monkeypatch):
    config = tmp_path / "Caddyfile"
    config.write_text(":9000 { respond ok }\n", encoding="utf-8")
    local = tmp_path / "dashboard" / "proxy" / "Caddyfile"
    local.parent.mkdir(parents=True)
    local.write_text(config.read_text(encoding="utf-8"), encoding="utf-8")
    result = subprocess.CompletedProcess(["ps"], 0, "caddy run --config " + str(config) + "\n", "")
    monkeypatch.setattr(routing.subprocess, "run", lambda *args, **kwargs: result)
    assert routing._process_config("caddy") == config
    assert routing._proxy_process_running("caddy")
    assert local in routing._conventional_configs(tmp_path, "caddy")


def test_validate_reload_report_proxy_errors(tmp_path, monkeypatch):
    config = tmp_path / "Caddyfile"
    config.write_text(":9000 { respond ok }\n", encoding="utf-8")
    proxy = routing.Proxy("caddy", "/fake/caddy", config)
    monkeypatch.setattr(
        routing, "_run", lambda command: subprocess.CompletedProcess(command, 1, "", "bad")
    )
    with pytest.raises(routing.RoutingError, match="validation failed"):
        routing._validate(proxy, config)
    with pytest.raises(routing.RoutingError, match="reload failed"):
        routing._reload(proxy, config)


def test_recover_prepared_journal_restores_original_and_detects_concurrent_edit(
    tmp_path, monkeypatch
):
    config = tmp_path / "Caddyfile"
    original = ":9000 { respond ok }\n"
    installed = original + 'import "snippet"\n'
    config.write_text(installed, encoding="utf-8")
    backup = config.parent / "incident-dashboard" / "caddy-8443.config-before"
    backup.parent.mkdir()
    backup.write_text(original, encoding="utf-8")
    backup.chmod(0o600)
    snippet = config.parent / "incident-dashboard" / "caddy-8443.conf"
    snippet.parent.mkdir(exist_ok=True)
    snippet.write_text("owned", encoding="utf-8")
    journal = {
        "state": "prepared",
        "operation": "open",
        "proxy": "caddy",
        "port": 8443,
        "config": str(config),
        "backup": str(backup),
        "snippet": str(snippet),
        "installed_config_hash": routing.hashlib.sha256(installed.encode()).hexdigest(),
        "original_config_hash": routing.hashlib.sha256(original.encode()).hexdigest(),
    }
    state_path = tmp_path / "routing.json"
    public_path = tmp_path / "public.json"
    state_path.write_text(json.dumps(journal), encoding="utf-8")
    monkeypatch.setattr(routing, "_executable", lambda name: "/fake/caddy")
    monkeypatch.setattr(routing, "_validate", lambda *args: None)
    monkeypatch.setattr(routing, "_reload", lambda *args: None)
    routing._recover_prepared(state_path, public_path, journal)
    assert config.read_text(encoding="utf-8") == original
    assert not state_path.exists()
    config.write_text("concurrent\n", encoding="utf-8")
    state_path.write_text(json.dumps(journal), encoding="utf-8")
    with pytest.raises(routing.RoutingError, match="concurrent"):
        routing._recover_prepared(state_path, public_path, journal)


def test_routing_low_level_rejections_and_filesystem_edges(tmp_path, monkeypatch):
    assert not routing._valid_host(".example.test")
    assert not routing._valid_host("example.test.")
    with pytest.raises(routing.RoutingError, match="integer"):
        routing._port("wat")
    malformed = tmp_path / "not-object.json"
    malformed.write_text("[]", encoding="utf-8")
    with pytest.raises(routing.RoutingError, match="malformed"):
        routing._read_journal(malformed)
    with pytest.raises(routing.RoutingError, match="unsupported"):
        routing._snippet("haproxy", "example.test", 443, 8766, "s", None, None)
    with pytest.raises(routing.RoutingError, match="unbalanced"):
        routing._include("http {", "nginx", tmp_path / "snippet.conf")
    config = tmp_path / "Caddyfile"
    config.write_text(":9000 { respond ok }\n", encoding="utf-8")
    info = config.stat()
    snippet_dir = config.parent / "incident-dashboard"
    snippet_dir.symlink_to(tmp_path / "other", target_is_directory=True)
    with pytest.raises(routing.RoutingError, match="symlinked"):
        routing._snippet_path(config, info, "caddy", 8443)
    snippet_dir.unlink()
    snippet_dir.write_text("file", encoding="utf-8")
    with pytest.raises(routing.RoutingError, match="not a directory"):
        routing._snippet_path(config, info, "caddy", 8443)
    snippet_dir.unlink()
    monkeypatch.setattr(
        routing.os,
        "chown",
        lambda *args: (_ for _ in ()).throw(OSError("denied")),
    )
    with pytest.raises(routing.RoutingError, match="proxy-readable"):
        routing._snippet_path(config, info, "caddy", 8443)


def test_atomic_owner_and_chown_failures(tmp_path, monkeypatch):
    target = tmp_path / "owned"
    target.write_text("old", encoding="utf-8")
    monkeypatch.setattr(routing.os, "geteuid", lambda: 99999)
    with pytest.raises(routing.RoutingError, match="preserve ownership"):
        routing._atomic(target, "new", owner=(1000, 1000))
    monkeypatch.setattr(routing.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        routing.os,
        "fchown",
        lambda *args: (_ for _ in ()).throw(OSError("denied")),
    )
    with pytest.raises(routing.RoutingError, match="preserve ownership"):
        routing._atomic(target, "new", owner=(1000, 1000))


def test_proxy_discovery_and_admin_read_failures(tmp_path, monkeypatch):
    config = tmp_path / "Caddyfile"
    config.write_text(":9000 { respond ok }\n", encoding="utf-8")
    monkeypatch.setattr(
        routing.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("missing")),
    )
    assert routing._process_config("caddy") is None
    assert not routing._proxy_process_running("caddy")
    unreadable = tmp_path / "unreadable"
    unreadable.write_text("admin localhost:1234\n", encoding="utf-8")
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self, **kwargs: (
            (_ for _ in ()).throw(OSError("denied"))
            if self == unreadable
            else Path.read_text(self, **kwargs)
        ),
    )
    assert routing._caddy_admin(unreadable) is None


def test_close_retry_removes_route_after_reload_failure(tmp_path, monkeypatch):
    config = tmp_path / "Caddyfile"
    original = ":9000 { respond ok }\n"
    config.write_text(original, encoding="utf-8")
    monkeypatch.setattr(routing, "_executable", lambda name: "/fake/caddy")
    monkeypatch.setattr(routing, "_occupied", lambda port, host: False)
    monkeypatch.setattr(routing, "_bind_occupied", lambda port: False)
    monkeypatch.setattr(
        routing,
        "_run",
        lambda command: subprocess.CompletedProcess(command, 0, "", ""),
    )
    calls = 0

    def reload_with_one_failure(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise routing.RoutingError("reload failed")

    monkeypatch.setattr(routing, "_reload", reload_with_one_failure)
    assert routing.run_port_command(ns(proxy_config=config), tmp_path) == 0
    close = ns(target="close", proxy=None, proxy_config=None)
    assert routing.run_port_command(close, tmp_path) == 1
    assert "incident-harness" in config.read_text(encoding="utf-8")
    assert routing.run_port_command(close, tmp_path) == 0
    assert config.read_text(encoding="utf-8") == original


def test_prepared_recovery_reloads_restored_proxy(tmp_path, monkeypatch):
    config = tmp_path / "Caddyfile"
    config.write_text(":9000 { respond ok }\n", encoding="utf-8")
    monkeypatch.setattr(routing, "_executable", lambda name: "/fake/caddy")
    monkeypatch.setattr(routing, "_occupied", lambda port, host: False)
    monkeypatch.setattr(routing, "_bind_occupied", lambda port: False)
    monkeypatch.setattr(
        routing,
        "_run",
        lambda command: subprocess.CompletedProcess(command, 0, "", ""),
    )
    monkeypatch.setattr(routing, "_probe", lambda *args, **kwargs: None)
    assert routing.run_port_command(ns(proxy_config=config), tmp_path) == 0
    state_path = tmp_path / "dashboard/routing.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["state"] = "prepared"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    calls = []
    monkeypatch.setattr(routing, "_reload", lambda *args: calls.append(args))
    routing._recover_prepared(state_path, tmp_path / "dashboard/public.json", state)
    assert calls


def test_final_contract_write_failure_rolls_back_proxy(tmp_path, monkeypatch):
    config = tmp_path / "Caddyfile"
    original = ":9000 { respond ok }\n"
    config.write_text(original, encoding="utf-8")
    monkeypatch.setattr(routing, "_executable", lambda name: "/fake/caddy")
    monkeypatch.setattr(routing, "_occupied", lambda port, host: False)
    monkeypatch.setattr(routing, "_bind_occupied", lambda port: False)
    monkeypatch.setattr(
        routing,
        "_run",
        lambda command: subprocess.CompletedProcess(command, 0, "", ""),
    )
    monkeypatch.setattr(routing, "_probe", lambda *args, **kwargs: None)
    original_atomic_dashboard = routing._atomic_dashboard
    writes = 0

    def fail_final_contract(path, data):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("disk full")
        original_atomic_dashboard(path, data)

    monkeypatch.setattr(routing, "_atomic_dashboard", fail_final_contract)
    with pytest.raises(routing.RoutingError, match="rolled back"):
        routing.run_port_command(ns(proxy_config=config), tmp_path)
    assert config.read_text(encoding="utf-8") == original
    assert not (tmp_path / "dashboard/routing.json").exists()
    assert json.loads((tmp_path / "dashboard/public.json").read_text())["enabled"] is False


def test_additional_transaction_and_probe_edges(tmp_path, monkeypatch):
    config = tmp_path / "Caddyfile"
    config.write_text(":9000 { respond ok }\n", encoding="utf-8")
    assert (
        routing._include(
            '# incident-harness dashboard include: /tmp/a\nimport "/tmp/a"\n',
            "caddy",
            Path("/tmp/a"),
        ).count("incident-harness")
        == 1
    )
    with pytest.raises(routing.RoutingError, match="non-canonical"):
        routing._proxy_from_journal({"config": "relative", "proxy": "caddy"}, ns(), tmp_path)
    original_sha = routing._sha
    monkeypatch.setattr(routing, "_sha", lambda path: "different")
    with pytest.raises(routing.RoutingError, match="concurrently"):
        routing._rollback(
            routing.Proxy("caddy", "/fake/caddy", config),
            config.read_text(encoding="utf-8"),
            installed_hash="installed",
            mode=0o644,
            owner=(config.stat().st_uid, config.stat().st_gid),
            backup=tmp_path / "backup",
        )
    monkeypatch.setattr(routing, "_sha", original_sha)

    class FakeStream:
        def settimeout(self, value):
            del value

        def sendall(self, value):
            del value

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeTLS:
        def wrap_socket(self, raw, server_hostname):
            del raw, server_hostname
            return FakeStream()

    class FakeResponse:
        status = 200

        def __init__(self, stream):
            del stream

        def begin(self):
            return None

        def read(self, size):
            del size
            return b"not-json"

    monkeypatch.setattr(routing.ssl, "_create_unverified_context", lambda: FakeTLS())
    monkeypatch.setattr(routing.socket, "create_connection", lambda *args, **kwargs: object())
    monkeypatch.setattr(routing.http.client, "HTTPResponse", FakeResponse)
    with pytest.raises(routing.RoutingError, match="did not reach"):
        routing._probe(
            routing.Proxy("caddy", "/fake/caddy", config),
            "example.test",
            8443,
            "secret",
            expect_dashboard=True,
        )


def test_privileged_binding_rejects_runtime_journal_tampering(tmp_path, monkeypatch):
    config = tmp_path / "Caddyfile"
    config.write_text(":9000 { respond ok }\n", encoding="utf-8")
    monkeypatch.setattr(routing, "_executable", lambda name: "/fake/caddy")
    monkeypatch.setattr(routing, "_occupied", lambda port, host: False)
    monkeypatch.setattr(routing, "_bind_occupied", lambda port: False)
    monkeypatch.setattr(
        routing, "_run", lambda command: subprocess.CompletedProcess(command, 0, "", "")
    )
    monkeypatch.setattr(routing, "_probe", lambda *args, **kwargs: None)
    assert routing.run_port_command(ns(proxy_config=config), tmp_path) == 0
    state_path = tmp_path / "dashboard/routing.json"
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    binding = routing._binding_path(tmp_path, saved)
    assert binding.is_file()

    monkeypatch.setattr(routing.os, "geteuid", lambda: 0)
    monkeypatch.setattr(routing, "_assert_privileged_path", lambda path: None)
    for field, value in (
        ("config", str(tmp_path / "other.Caddyfile")),
        ("snippet", str(tmp_path / "other.conf")),
        ("backup", str(tmp_path / "other.backup")),
        ("original_config_hash", "tampered"),
    ):
        changed = dict(saved)
        changed[field] = value
        state_path.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(routing.RoutingError, match="binding|targets differ|hash"):
            routing.run_port_command(ns(target="close", proxy=None, proxy_config=None), tmp_path)
        assert "incident-harness" in config.read_text(encoding="utf-8")
    state_path.write_text(json.dumps(saved), encoding="utf-8")
    binding.unlink()
    with pytest.raises(routing.RoutingError, match="regular|missing"):
        routing.run_port_command(ns(target="close", proxy=None, proxy_config=None), tmp_path)


def test_privileged_path_rejects_user_owned_files(tmp_path):
    config = tmp_path / "user.Caddyfile"
    config.write_text("events {}\n", encoding="utf-8")
    with pytest.raises(routing.RoutingError, match="root-controlled"):
        routing._assert_privileged_path(config)


def test_protected_binding_acceptance_and_hash_tamper_checks(tmp_path, monkeypatch):
    config = tmp_path / "Caddyfile"
    config.write_text(":9000 { respond ok }\n", encoding="utf-8")
    monkeypatch.setattr(routing, "_executable", lambda name: "/fake/caddy")
    monkeypatch.setattr(routing, "_occupied", lambda port, host: False)
    monkeypatch.setattr(routing, "_bind_occupied", lambda port: False)
    monkeypatch.setattr(
        routing, "_run", lambda command: subprocess.CompletedProcess(command, 0, "", "")
    )
    monkeypatch.setattr(routing, "_probe", lambda *args, **kwargs: None)
    assert routing.run_port_command(ns(proxy_config=config), tmp_path) == 0
    state_path = tmp_path / "dashboard/routing.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    binding = routing._binding_path(tmp_path, state)
    backup = Path(state["backup"])
    original_backup = backup.read_bytes()
    monkeypatch.setattr(routing.os, "geteuid", lambda: 0)
    monkeypatch.setattr(routing, "_assert_privileged_path", lambda path: None)
    routing._validate_binding(tmp_path, state)
    wrong_hash = dict(state, config_hash="wrong")
    state_path.write_text(json.dumps(wrong_hash), encoding="utf-8")
    with pytest.raises(routing.RoutingError, match="configuration hash"):
        routing.run_port_command(ns(target="close", proxy=None, proxy_config=None), tmp_path)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    backup.write_bytes(original_backup + b"tamper")
    with pytest.raises(routing.RoutingError, match="backup differs"):
        routing.run_port_command(ns(target="close", proxy=None, proxy_config=None), tmp_path)
    backup.write_bytes(original_backup)
    routing._write_binding(tmp_path, state)
    assert binding.is_file()


def test_binding_path_and_probe_failure_branches(tmp_path, monkeypatch):
    with pytest.raises(routing.RoutingError, match="canonical"):
        routing._binding_path(tmp_path, {"config": "relative", "proxy": "caddy", "port": 8443})
    with pytest.raises(routing.RoutingError, match="canonical"):
        routing._binding_path(
            tmp_path,
            {"config": str(tmp_path / "Caddyfile"), "proxy": "haproxy", "port": 8443},
        )
    config = tmp_path / "Caddyfile"
    config.write_text(":9000 { respond ok }\n", encoding="utf-8")
    monkeypatch.setattr(routing.ssl, "_create_unverified_context", lambda: object())
    monkeypatch.setattr(
        routing.socket,
        "create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("down")),
    )
    ticks = iter((0.0, 99.0))
    monkeypatch.setattr(routing.time, "monotonic", lambda: next(ticks))
    with pytest.raises(routing.RoutingError, match="probe failed"):
        routing._probe(
            routing.Proxy("caddy", "/fake/caddy", config),
            "example.test",
            8443,
            "secret",
            expect_dashboard=True,
        )


def test_prepared_recovery_without_backup_reloads_original(tmp_path, monkeypatch):
    config = tmp_path / "Caddyfile"
    original = ":9000 { respond ok }\n"
    config.write_text(original, encoding="utf-8")
    monkeypatch.setattr(routing, "_executable", lambda name: "/fake/caddy")
    monkeypatch.setattr(routing, "_occupied", lambda port, host: False)
    monkeypatch.setattr(routing, "_bind_occupied", lambda port: False)
    monkeypatch.setattr(
        routing, "_run", lambda command: subprocess.CompletedProcess(command, 0, "", "")
    )
    monkeypatch.setattr(routing, "_probe", lambda *args, **kwargs: None)
    assert routing.run_port_command(ns(proxy_config=config), tmp_path) == 0
    state_path = tmp_path / "dashboard/routing.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    config.write_text(original, encoding="utf-8")
    Path(state["backup"]).unlink()
    state["state"] = "prepared"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    reloaded = []
    monkeypatch.setattr(routing, "_validate", lambda *args: None)
    monkeypatch.setattr(routing, "_reload", lambda *args: reloaded.append(True))
    routing._recover_prepared(state_path, tmp_path / "dashboard/public.json", state)
    assert reloaded and not state_path.exists()


def test_prepared_recovery_without_executable_retains_journal(tmp_path, monkeypatch):
    config = tmp_path / "Caddyfile"
    config.write_text(":9000 { respond ok }\n", encoding="utf-8")
    monkeypatch.setattr(routing, "_executable", lambda name: "/fake/caddy")
    monkeypatch.setattr(routing, "_occupied", lambda port, host: False)
    monkeypatch.setattr(routing, "_bind_occupied", lambda port: False)
    monkeypatch.setattr(
        routing, "_run", lambda command: subprocess.CompletedProcess(command, 0, "", "")
    )
    monkeypatch.setattr(routing, "_probe", lambda *args, **kwargs: None)
    assert routing.run_port_command(ns(proxy_config=config), tmp_path) == 0
    state_path = tmp_path / "dashboard/routing.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["state"] = "prepared"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(routing, "_executable", lambda name: None)
    with pytest.raises(routing.RoutingError, match="executable"):
        routing._recover_prepared(state_path, tmp_path / "dashboard/public.json", state)
    assert state_path.exists()


def test_install_exception_after_replace_rolls_back(tmp_path, monkeypatch):
    config = tmp_path / "Caddyfile"
    original = ":9000 { respond ok }\n"
    config.write_text(original, encoding="utf-8")
    monkeypatch.setattr(routing, "_executable", lambda name: "/fake/caddy")
    monkeypatch.setattr(routing, "_occupied", lambda port, host: False)
    monkeypatch.setattr(routing, "_bind_occupied", lambda port: False)
    monkeypatch.setattr(
        routing, "_run", lambda command: subprocess.CompletedProcess(command, 0, "", "")
    )
    monkeypatch.setattr(routing, "_probe", lambda *args, **kwargs: None)
    original_atomic = routing._atomic
    failed = False

    def atomic_then_fail(path, data, *args, **kwargs):
        nonlocal failed
        original_atomic(path, data, *args, **kwargs)
        if path == config and not failed and "incident-harness" in data:
            failed = True
            raise OSError("post-install failure")

    monkeypatch.setattr(routing, "_atomic", atomic_then_fail)
    with pytest.raises(routing.RoutingError, match="rolled back"):
        routing.run_port_command(ns(proxy_config=config), tmp_path)
    assert config.read_text(encoding="utf-8") == original


def test_retired_binding_cannot_replay_stale_runtime_after_route_reuse(tmp_path, monkeypatch):
    config = tmp_path / "Caddyfile"
    config.write_text(":9000 { respond ok }\n", encoding="utf-8")
    runtime_a = tmp_path / "runtime-a"
    runtime_b = tmp_path / "runtime-b"
    for runtime in (runtime_a, runtime_b):
        (runtime / "dashboard").mkdir(parents=True)
    monkeypatch.setattr(routing, "_executable", lambda name: "/fake/caddy")
    monkeypatch.setattr(routing, "_occupied", lambda port, host: False)
    monkeypatch.setattr(routing, "_bind_occupied", lambda port: False)
    monkeypatch.setattr(
        routing, "_run", lambda command: subprocess.CompletedProcess(command, 0, "", "")
    )
    monkeypatch.setattr(routing, "_probe", lambda *args, **kwargs: None)
    args = ns(proxy_config=config)
    assert routing.run_port_command(args, runtime_a) == 0
    state_a = json.loads((runtime_a / "dashboard/routing.json").read_text(encoding="utf-8"))
    assert (
        routing.run_port_command(ns(target="close", proxy=None, proxy_config=None), runtime_a) == 0
    )
    assert json.loads(routing._binding_path(runtime_a, state_a).read_text())["retired"] is True

    assert routing.run_port_command(args, runtime_b) == 0
    state_b = json.loads((runtime_b / "dashboard/routing.json").read_text(encoding="utf-8"))
    b_snippet = Path(state_b["snippet"])
    b_backup = Path(state_b["backup"])
    before_config = config.read_bytes()
    before_snippet = b_snippet.read_bytes()
    before_backup = b_backup.read_bytes()

    monkeypatch.setattr(routing.os, "geteuid", lambda: 0)
    monkeypatch.setattr(routing, "_assert_privileged_path", lambda path: None)
    (runtime_a / "dashboard/routing.json").write_text(json.dumps(state_a), encoding="utf-8")
    with pytest.raises(routing.RoutingError, match="retired"):
        routing.run_port_command(ns(target="close", proxy=None, proxy_config=None), runtime_a)
    assert config.read_bytes() == before_config
    assert b_snippet.read_bytes() == before_snippet
    assert b_backup.read_bytes() == before_backup
