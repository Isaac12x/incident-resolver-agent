from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from src.bundles import (
    activate_bundle,
    build_bundle,
    list_bundles,
    load_active_bundle,
    rollback_bundle,
)
from src.config import Config
from src.lifecycle import doctor, ensure_runtime_tools, require_ready, update_installation


def test_bundle_snapshots_inputs_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    skill = tmp_path / "skills"
    skill.mkdir()
    (skill / "SKILL.md").write_text("first", encoding="utf-8")
    config = Config(
        runtime_root=tmp_path / "state",
        agent={"system_prompt": "prompt", "skill_directories": ["skills"]},
    )
    first = build_bundle(config)
    assert (first.path / "prompt.txt").read_text() == "prompt"
    assert (first.path / "connections.json").is_file()
    activate_bundle(config, first.version)
    (skill / "SKILL.md").write_text("second", encoding="utf-8")
    second = build_bundle(config)
    assert first.version != second.version
    activate_bundle(config, second.version)
    assert load_active_bundle(config).version == second.version
    assert rollback_bundle(config).version == first.version
    assert [item.version for item in list_bundles(config)] == sorted(
        (first.version, second.version)
    )


def test_bundle_activation_rejects_unknown_and_no_rollback(tmp_path: Path) -> None:
    config = Config(runtime_root=tmp_path / "state", agent={"skill_directories": []})
    with pytest.raises(FileNotFoundError):
        activate_bundle(config, "missing")
    with pytest.raises(RuntimeError):
        rollback_bundle(config)


def test_doctor_reports_missing_config_and_credentials(tmp_path: Path) -> None:
    missing = doctor(tmp_path / "missing.toml")
    assert not missing[0].ok
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[model]\nbase_url='https://example.test/v1'\napi_key_env='MISSING_KEY'\n",
        encoding="utf-8",
    )
    checks = doctor(config_path)
    assert any(check.name == "model credentials" and not check.ok for check in checks)
    with pytest.raises(RuntimeError, match="run 'incident-agent doctor'"):
        require_ready(config_path)


def test_doctor_container_image_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[execution]\nmode='container'\nimage='x:test'\n", encoding="utf-8")
    monkeypatch.setattr(
        "src.lifecycle.shutil.which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    checks = doctor(
        config_path,
        runner=lambda *args, **kwargs: CompletedProcess(args[0], 1, "", "missing"),
    )
    assert any(check.name == "container image" and not check.ok for check in checks)


def test_doctor_invalid_and_missing_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    invalid = tmp_path / "invalid.toml"
    invalid.write_text("[model\n", encoding="utf-8")
    assert not doctor(invalid)[0].ok
    config_path = tmp_path / "container.toml"
    config_path.write_text("[execution]\nmode='container'\n", encoding="utf-8")
    monkeypatch.setattr("src.lifecycle.shutil.which", lambda _: None)
    assert any(check.name == "container runtime" and not check.ok for check in doctor(config_path))


def test_require_ready_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[model]\nbase_url='https://example.test/v1'\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    require_ready(config_path)


def test_doctor_checks_subscription_connector_and_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[model]\nruntime='subscription-cli'\nsubscription_command=['fake-cli']\n"
        "[[connectors]]\nname='mcp'\npurpose='other'\ntype='mcp'\ntransport='stdio'\n"
        "command=['helper']\nauth_token_env='MCP_TOKEN'\n"
        "[[repositories]]\nname='owner/repo'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.lifecycle.shutil.which",
        lambda name: "/usr/bin/fake" if name in {"fake-cli", "helper"} else None,
    )
    checks = doctor(
        config_path,
        runner=lambda *args, **kwargs: CompletedProcess(args[0], 0, "ready", ""),
    )
    assert any(check.name == "model runtime" and check.ok for check in checks)
    assert any(check.name == "connector mcp executable" and check.ok for check in checks)
    assert any(check.name == "connector mcp" and not check.ok for check in checks)
    assert any(check.name == "repository owner/repo" and not check.ok for check in checks)


def test_managed_tool_update_failure_and_permission_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[[repositories]]\nname='owner/repo'\nclone_url='https://example.test/repo.git'\n"
        "[permissions]\nallow_dependency_installation=false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.tooling.executable_status", lambda _: {"seed": False, "code-review-graph": False}
    )
    with pytest.raises(RuntimeError, match="disabled"):
        ensure_runtime_tools(config_path)
    monkeypatch.setattr("src.lifecycle.shutil.which", lambda _: "/usr/bin/uv")
    result = update_installation(
        managed_tools=("seed",),
        runner=lambda command, **kwargs: CompletedProcess(
            command, 1 if command[-1] == "seed-cli" else 0, "", "failed"
        ),
    )
    assert result.returncode == 1


def test_bundle_rejects_inline_credentials_and_tampering(tmp_path: Path) -> None:
    config = Config(
        runtime_root=tmp_path / "state",
        agent={"skill_directories": []},
        connectors=[
            {
                "name": "unsafe",
                "purpose": "other",
                "type": "mcp",
                "transport": "streamable-http",
                "url": "https://example.test/mcp?token=secret",
            }
        ],
    )
    with pytest.raises(ValueError, match="inline credentials"):
        build_bundle(config)
    safe = Config(runtime_root=tmp_path / "safe", agent={"skill_directories": []})
    bundle = build_bundle(safe)
    (bundle.path / "prompt.txt").write_text("tampered", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        activate_bundle(safe, bundle.version)


def test_rollback_selects_most_recent_activation_not_oldest(tmp_path):
    config = Config(runtime_root=tmp_path / "state")
    versions = []
    for prompt in ("oldest", "previous", "current"):
        config.agent.system_prompt = prompt
        bundle = build_bundle(config)
        activate_bundle(config, bundle.version)
        versions.append(bundle.version)
    assert rollback_bundle(config).version == versions[1]


def test_nested_unexpected_manifest_invalidates_bundle(tmp_path):
    config = Config(runtime_root=tmp_path / "state")
    bundle = build_bundle(config)
    nested = bundle.path / "skills" / "unexpected" / "manifest.json"
    nested.parent.mkdir(parents=True)
    nested.write_text("{}")
    assert list_bundles(config) == []


def test_helper_resolution_keeps_interpreter_environment(tmp_path, monkeypatch):
    import sys

    from src.tooling import _tool_executable, executable_status, install_uv_tools

    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "python").symlink_to(sys.executable)
    (bindir / "seed").write_text("helper")
    monkeypatch.setattr(sys, "executable", str(bindir / "python"))
    monkeypatch.setattr(
        "src.tooling.shutil.which", lambda name: "/bin/uv" if name == "uv" else None
    )
    assert _tool_executable("seed") == str(bindir / "seed")
    assert executable_status(["seed"])["seed"]
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        return CompletedProcess(command, 0, "", "")

    install_uv_tools(["seed"], runner=runner)
    assert commands[0][-1] == "seed-cli"
    update_installation(managed_tools=("seed",), runner=runner)
    assert commands[-1][-2:] == ["--upgrade", "seed-cli"]
