"""Grouped CLI help is the default helper, not a missing-command error."""

from __future__ import annotations

import pytest

from src.__main__ import parse_arguments
from src.cli_help import format_cli_help, overview_next_steps, suggest_command


def test_catalog_covers_every_parser_command() -> None:
    help_text = format_cli_help()
    assert "Setup" in help_text
    assert "Run" in help_text
    for name in (
        "init",
        "config, tui",
        "plugins",
        "connect",
        "doctor",
        "run",
        "dashboard",
        "executions",
        "bundle",
        "eval",
        "install-repositories",
    ):
        assert name in help_text
    assert "incident-agent COMMAND --help" in help_text


def test_bare_invocation_prints_grouped_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments([])
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "Durable agent harness" in output
    assert "Setup" in output
    assert "incident-agent init" in output
    assert "{init,serve" not in output
    assert "the following arguments are required" not in output
    assert "COMMAND" in output


def test_help_flag_uses_grouped_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments(["--help"])
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "config, tui" in output
    assert "dashboard" in output
    assert "{init,serve" not in output


def test_unknown_command_suggests_close_match(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments(["serv"])
    assert error.value.code == 2
    err = capsys.readouterr().err
    assert "unknown command 'serv'" in err
    assert "Did you mean" in err
    assert "serve" in err
    assert "Try 'incident-agent --help'" in err
    assert "{init,serve" not in err


def test_dashboard_help_lists_actions(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments(["dashboard", "--help"])
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "port close" in output
    assert "incident-agent dashboard -d" in output


def test_executions_help_lists_inspect(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments(["executions", "--help"])
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "inspect" in output
    assert "jack-in" not in output


def test_run_help_explains_optional_file(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments(["run", "--help"])
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "FILE" in output
    assert "Omit FILE" in output


def test_main_without_command_does_not_bootstrap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from src import __main__ as entry

    monkeypatch.setattr(entry, "bootstrap", lambda *_args, **_kwargs: pytest.fail("no bootstrap"))
    with pytest.raises(SystemExit) as error:
        entry.main([])
    assert error.value.code == 0
    assert "Setup" in capsys.readouterr().out


def test_invalid_option_hints_help_without_guessing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        parse_arguments(["healthcheck", "--timeout", "nope"])
    assert error.value.code == 2
    err = capsys.readouterr().err
    assert "Did you mean" not in err
    assert "Try 'incident-agent healthcheck --help'" in err


def test_next_steps_and_suggestions() -> None:
    assert "incident-agent run" in overview_next_steps(ready=True)
    assert "incident-agent doctor" in overview_next_steps(ready=False)
    assert suggest_command("initt") == "Did you mean init?"
    assert "serve" in (suggest_command("serv") or "")
    assert suggest_command("zzzzzzzz") is None
