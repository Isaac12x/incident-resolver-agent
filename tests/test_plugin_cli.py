"""Focused tests for the read-only plugin catalog and connector CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import __main__ as cli
from src.config import load_config


def test_plugins_lists_catalog_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["plugins", "--json"])
    rendered = json.loads(capsys.readouterr().out)
    assert {item["name"] for item in rendered} == {
        "mcp",
        "webhook",
        "loki",
        "grafana",
        "local-logs",
    }


def test_plugins_lists_human_catalog(capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["plugins", "list"])
    output = capsys.readouterr().out
    assert "mcp:" in output
    assert "[logs]" in output


def test_connect_adds_two_named_instances_and_lists_secret_free(tmp_path: Path, capsys) -> None:
    path = tmp_path / "config.toml"
    cli.main(["--config", str(path), "connect", "webhook", "--name", "alerts-a"])
    cli.main(["--config", str(path), "connect", "webhook", "--name", "alerts-b"])
    cli.main(["--config", str(path), "connect", "--list"])
    assert "alerts-a\twebhook" in capsys.readouterr().out
    assert [item.name for item in load_config(path, create=False).connectors] == [
        "alerts-a",
        "alerts-b",
    ]


def test_connect_list_hides_endpoint_and_secret_values(tmp_path: Path, capsys) -> None:
    path = tmp_path / "config.toml"
    cli.main(
        [
            "--config",
            str(path),
            "connect",
            "mcp",
            "--name",
            "tools",
            "--url",
            "https://private.example/secret-endpoint",
            "--auth-token-env",
            "PRIVATE_TOKEN_ENV",
            "--capability",
            "logs",
        ]
    )
    capsys.readouterr()
    cli.main(["--config", str(path), "connect", "--list"])
    output = capsys.readouterr().out
    assert "tools\tmcp\tlogs" in output
    assert "private.example" not in output
    assert "PRIVATE_TOKEN_ENV" not in output


def test_duplicate_connect_does_not_mutate_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    cli.main(["--config", str(path), "connect", "webhook", "--name", "alerts"])
    before = path.read_text()
    with pytest.raises(SystemExit, match="already configured"):
        cli.main(["--config", str(path), "connect", "webhook", "--name", "alerts"])
    assert path.read_text() == before


def test_missing_name_and_bad_arguments_fail_without_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    with pytest.raises(SystemExit, match="requires --name"):
        cli.main(["--config", str(path), "connect", "webhook"])
    assert not path.exists()
    with pytest.raises(SystemExit):
        cli.parse_arguments(["connect", "webhook", "--transport", "invalid"])


def test_connect_errors_are_field_specific_and_does_not_write(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    with pytest.raises(SystemExit, match=r"HTTP\(S\) URL"):
        cli.main(["--config", str(path), "connect", "loki", "--name", "logs"])
    assert not path.exists()


def test_connect_rejects_list_options_and_unknown_plugin(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="cannot be combined"):
        cli.main(["--config", str(tmp_path / "config.toml"), "connect", "--list", "--name", "x"])
    with pytest.raises(SystemExit, match="unknown source adapter"):
        cli.main(["--config", str(tmp_path / "config.toml"), "connect", "unknown", "--name", "x"])


def test_interactive_connect_uses_selected_adapter(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    monkeypatch.setattr(cli, "_interactive_connect", lambda catalog: ("webhook", "interactive", {}))
    cli.main(["--config", str(path), "connect"])
    assert load_config(path, create=False).connectors[0].name == "interactive"


def test_interactive_non_tty_fails_concisely(monkeypatch) -> None:
    class NonTTY:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(cli.sys, "stdin", NonTTY())
    monkeypatch.setattr(cli.sys, "stdout", NonTTY())
    with pytest.raises(SystemExit, match="non-interactive"):
        cli.main(["connect"])


def test_interactive_cancelled_and_invalid_selection(monkeypatch) -> None:
    original_interactive = cli._interactive_connect
    monkeypatch.setattr(
        cli, "_interactive_connect", lambda _catalog: (_ for _ in ()).throw(EOFError())
    )
    with pytest.raises(SystemExit, match="cancelled"):
        cli.main(["connect"])
    monkeypatch.setattr(cli, "_interactive_connect", original_interactive)

    class TTY:
        def isatty(self) -> bool:
            return True

        def write(self, value: str) -> int:
            return len(value)

        def flush(self) -> None:
            return None

    monkeypatch.setattr(cli.sys, "stdin", TTY())
    monkeypatch.setattr(cli.sys, "stdout", TTY())
    monkeypatch.setattr("builtins.input", lambda _prompt="": "0")
    with pytest.raises(SystemExit, match="invalid adapter"):
        cli.main(["connect"])


def test_interactive_mcp_requires_capability(monkeypatch) -> None:
    class TTY:
        def isatty(self) -> bool:
            return True

        def write(self, value: str) -> int:
            return len(value)

        def flush(self) -> None:
            return None

    monkeypatch.setattr(cli.sys, "stdin", TTY())
    monkeypatch.setattr(cli.sys, "stdout", TTY())
    answers = iter(["1", "tools", "streamable-http", "https://mcp.example", "", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    with pytest.raises(SystemExit, match="require at least one capability"):
        cli.main(["connect"])


def test_missing_default_config_uses_operational_defaults(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    runtime_path = tmp_path / "runtime"
    monkeypatch.setattr(cli, "default_config_path", lambda: config_path)
    monkeypatch.setattr(cli, "default_runtime_path", lambda: runtime_path)
    cli.main(["connect", "webhook", "--name", "alerts"])
    config = load_config(config_path, create=False)
    assert config.runtime_root == runtime_path
    assert config.server.require_api_auth is True


def test_invalid_config_list_fails_concisely(tmp_path: Path) -> None:
    path = tmp_path / "broken.toml"
    path.write_text("not valid = [", encoding="utf-8")
    with pytest.raises(SystemExit, match="could not read configuration"):
        cli.main(["--config", str(path), "connect", "--list"])


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        (["1", "mcp-http", "streamable-http", "https://mcp.example", "", "logs"], "mcp-http"),
        (["1", "mcp-stdio", "stdio", 'tool "with args"', "logs"], "mcp-stdio"),
        (["2", "hooks", ""], "hooks"),
        (["3", "loki", "https://loki.example", "", "logs"], "loki"),
        (["4", "grafana", "https://grafana.example", "ds", "", "logs"], "grafana"),
        (["5", "file", "/tmp/app.log", "logs"], "file"),
    ],
)
def test_interactive_connect_collects_type_specific_fields(
    monkeypatch, tmp_path: Path, answers: list[str], expected: str
) -> None:
    class TTY:
        def __init__(self, wrapped=None):
            self.wrapped = wrapped

        def isatty(self) -> bool:
            return True

        def write(self, value: str) -> int:
            return self.wrapped.write(value) if self.wrapped else len(value)

        def flush(self) -> None:
            if self.wrapped:
                self.wrapped.flush()

    monkeypatch.setattr(cli.sys, "stdin", TTY())
    monkeypatch.setattr(cli.sys, "stdout", TTY(cli.sys.stdout))
    values = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(values))
    path = tmp_path / f"{expected}.toml"
    cli.main(["--config", str(path), "connect"])
    assert load_config(path, create=False).connectors[0].name == expected


def test_interactive_rejects_unquoted_command_and_ignored_options(monkeypatch) -> None:
    with pytest.raises(SystemExit, match="requires PLUGIN"):
        cli.main(["connect", "--url", "https://example"])

    class TTY:
        def __init__(self, wrapped=None):
            self.wrapped = wrapped

        def isatty(self) -> bool:
            return True

        def write(self, value: str) -> int:
            return self.wrapped.write(value) if self.wrapped else len(value)

        def flush(self) -> None:
            if self.wrapped:
                self.wrapped.flush()

    monkeypatch.setattr(cli.sys, "stdin", TTY())
    monkeypatch.setattr(cli.sys, "stdout", TTY(cli.sys.stdout))
    answers = iter(["1", "stdio", "stdio", 'bad "quote', "logs"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    with pytest.raises(SystemExit, match="invalid stdio command"):
        cli.main(["connect"])
