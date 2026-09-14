from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock, patch

import pytest

from src.__main__ import main
from src.config import Config, load_config
from src.lifecycle import (
    bootstrap,
    default_config_path,
    default_runtime_path,
    ensure_user_config,
    update_installation,
)
from src.tooling import ToolResult


def test_default_paths_follow_xdg(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert default_config_path(tmp_path) == tmp_path / "config" / "incident-harness" / "config.toml"
    assert default_runtime_path() == tmp_path / "state" / "incident-harness"
    monkeypatch.setenv("INCIDENT_AGENT_CONFIG", "~/custom.toml")
    assert default_config_path(tmp_path) == Path("~/custom.toml").expanduser()


def test_checkout_config_wins_and_user_config_uses_state(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / ".agent").mkdir()
    assert default_config_path(tmp_path) == tmp_path / ".agent" / "config.toml"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = tmp_path / "user" / "config.toml"
    ensure_user_config(path)
    config = load_config(path)
    assert config.runtime_root == tmp_path / "state" / "incident-harness"
    assert bootstrap(tmp_path / "nested" / "config.toml") == tmp_path / "nested" / "config.toml"
    ensure_user_config(path)  # Existing config is deliberately left untouched.


def test_update_returns_actual_uv_result(monkeypatch) -> None:
    monkeypatch.setattr("src.lifecycle.shutil.which", lambda _: "/usr/bin/uv")
    expected = CompletedProcess(["uv"], 0, "updated\n", "")
    result = update_installation(runner=lambda *args, **kwargs: expected)
    assert result is expected


def test_update_requires_uv(monkeypatch) -> None:
    monkeypatch.setattr("src.lifecycle.shutil.which", lambda _: None)
    try:
        update_installation()
    except RuntimeError as error:
        assert "uv is required" in str(error)
    else:
        raise AssertionError("missing uv must fail")


def test_cli_eval_update_and_argument_free_run(tmp_path: Path, capsys) -> None:
    report = {"failed": 0, "total": 1}
    with patch("src.evals.run_evaluations", return_value=report):
        main(["eval"])
    assert '"total": 1' in capsys.readouterr().out
    output = tmp_path / "report.json"
    with patch("src.evals.run_evaluations", return_value=report):
        main(["eval", "--output", str(output)])
    assert output.exists()
    with patch("src.evals.run_evaluations", return_value={"failed": 1}), pytest.raises(
        SystemExit, match="1"
    ):
        main(["eval"])
    completed = CompletedProcess(["uv"], 0, "updated\n", "warning\n")
    with patch("src.__main__.update_installation", return_value=completed):
        main(["update"])
    assert "updated" in capsys.readouterr().out
    config = Config(runtime_root=tmp_path / "runtime")
    application = Mock(config=config)
    with (
        patch("src.__main__.Application.build", return_value=application),
        patch("src.__main__.create_server", return_value=Mock()),
        patch("src.__main__.uvicorn.run"),
    ):
        main(["--config", str(tmp_path / "config.toml"), "run"])


def test_cli_init_explicit_and_seed_failure(tmp_path: Path, monkeypatch) -> None:
    explicit = tmp_path / "explicit.toml"
    main(["--config", str(explicit), "init"])
    assert load_config(explicit).connectors[0].name == "grafana"
    (tmp_path / "source").mkdir()
    monkeypatch.chdir(tmp_path / "source")
    failure = ToolResult(("seed",), 4, "", "failed\n")
    with patch("src.__main__.initialise_runtime_tree", return_value=failure), pytest.raises(
        SystemExit, match="4"
    ):
        main(["init"])
