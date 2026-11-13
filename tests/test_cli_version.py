"""Regression coverage for the install/version CLI paths."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src import __main__ as cli
from src import version as version_module


def test_version_flag_prints_package_name_version_and_optional_revision(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        cli,
        "get_version_info",
        lambda: {"name": "incident-harness", "version": "1.2.3", "commit": "abc1234"},
    )

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == "incident-harness 1.2.3 (commit abc1234)"


def test_version_command_prints_human_version_without_revision_when_unknown(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        cli,
        "get_version_info",
        lambda: {"name": "incident-harness", "version": "unknown", "commit": None},
    )

    cli.main(["version"])

    assert capsys.readouterr().out.strip() == "incident-harness unknown"


def test_version_command_json_has_stable_machine_readable_shape(monkeypatch, capsys) -> None:
    info = {"name": "incident-harness", "version": "1.2.3", "commit": "abc1234"}
    monkeypatch.setattr(cli, "get_version_info", lambda: info)

    cli.main(["version", "--json"])

    assert json.loads(capsys.readouterr().out) == info


def test_version_command_does_not_load_config_or_bootstrap(monkeypatch, tmp_path, capsys) -> None:
    config_path = tmp_path / "does-not-exist.toml"
    monkeypatch.setattr(
        cli,
        "get_version_info",
        lambda: {"name": "incident-harness", "version": "1.2.3", "commit": None},
    )

    def unexpected(*_args, **_kwargs):
        raise AssertionError("version command must not load config or bootstrap")

    monkeypatch.setattr(cli, "load_config", unexpected)
    monkeypatch.setattr(cli, "bootstrap", unexpected)
    monkeypatch.setattr(cli, "ensure_user_config", unexpected)
    monkeypatch.setattr(cli, "ensure_runtime_tools", unexpected)
    monkeypatch.setattr(cli.urllib.request, "urlopen", unexpected)

    cli.main(["--config", str(config_path), "version"])

    assert "1.2.3" in capsys.readouterr().out
    assert not config_path.exists()


def test_version_info_uses_installed_distribution_metadata(monkeypatch) -> None:
    monkeypatch.setattr(
        version_module.metadata,
        "distribution",
        lambda name: SimpleNamespace(version="4.5.6", read_text=lambda _name: None),
    )

    assert version_module.get_version_info() == {
        "name": "incident-harness",
        "version": "4.5.6",
        "commit": None,
    }


def test_version_info_returns_unknown_when_distribution_metadata_is_missing(monkeypatch) -> None:
    def missing(_name: str) -> str:
        raise version_module.metadata.PackageNotFoundError("incident-harness")

    monkeypatch.setattr(version_module.metadata, "distribution", missing)

    assert version_module.get_version_info() == {
        "name": "incident-harness",
        "version": "unknown",
        "commit": None,
    }


def test_version_info_reads_git_revision_from_direct_url_metadata(monkeypatch) -> None:
    direct_url = json.dumps(
        {
            "url": "file:///workspace/incident-harness",
            "vcs_info": {"vcs": "git", "commit_id": "deadbeef", "requested_revision": "main"},
        }
    )
    monkeypatch.setattr(
        version_module.metadata,
        "distribution",
        lambda name: SimpleNamespace(version="4.5.6", read_text=lambda _name: direct_url),
    )

    assert version_module.get_version_info() == {
        "name": "incident-harness",
        "version": "4.5.6",
        "commit": "deadbeef",
    }


def test_malformed_optional_direct_url_metadata_is_ignored(monkeypatch) -> None:
    monkeypatch.setattr(
        version_module.metadata,
        "distribution",
        lambda name: SimpleNamespace(version="4.5.6", read_text=lambda _name: "{broken"),
    )

    assert version_module.get_version_info() == {
        "name": "incident-harness",
        "version": "4.5.6",
        "commit": None,
    }


def test_unreadable_optional_direct_url_metadata_is_ignored(monkeypatch) -> None:
    def unreadable(_name: str) -> str:
        raise OSError("broken metadata file")

    monkeypatch.setattr(
        version_module.metadata,
        "distribution",
        lambda name: SimpleNamespace(version="4.5.6", read_text=unreadable),
    )

    assert version_module.get_version_info() == {
        "name": "incident-harness",
        "version": "4.5.6",
        "commit": None,
    }


def test_non_object_optional_direct_url_metadata_is_ignored(monkeypatch) -> None:
    monkeypatch.setattr(
        version_module.metadata,
        "distribution",
        lambda name: SimpleNamespace(version="4.5.6", read_text=lambda _name: "[]"),
    )

    assert version_module.get_version_info() == {
        "name": "incident-harness",
        "version": "4.5.6",
        "commit": None,
    }
