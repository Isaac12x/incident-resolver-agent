"""Terminal-aware formatting for human CLI output."""

from __future__ import annotations

import io
import json

import pytest

from src import __main__ as cli
from src.lifecycle import Check


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


@pytest.mark.parametrize(
    ("tty", "env", "expected"),
    [
        (True, {}, True),
        (False, {}, False),
        (True, {"NO_COLOR": ""}, False),
        (True, {"NO_COLOR": "", "FORCE_COLOR": "1"}, False),
        (True, {"TERM": "dumb"}, False),
        (False, {"FORCE_COLOR": "1"}, True),
        (False, {"FORCE_COLOR": "0"}, False),
    ],
)
def test_color_support_respects_terminal_and_environment(
    monkeypatch: pytest.MonkeyPatch, tty: bool, env: dict[str, str], expected: bool
) -> None:
    from src.cli_format import supports_color

    for name in ("NO_COLOR", "TERM", "FORCE_COLOR"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    stream = _TTY() if tty else _Pipe()

    assert supports_color(stream) is expected


def test_color_wrapper_is_plain_for_non_tty_and_ansi_for_tty(monkeypatch) -> None:
    from src.cli_format import color

    for name in ("NO_COLOR", "TERM", "FORCE_COLOR"):
        monkeypatch.delenv(name, raising=False)
    assert color("OK", "32", _Pipe()) == "OK"
    assert color("OK", "32", _TTY()) == "\x1b[32mOK\x1b[0m"


def test_render_table_has_aligned_columns_and_plain_piped_output(monkeypatch) -> None:
    from src.cli_format import render_table

    for name in ("NO_COLOR", "TERM", "FORCE_COLOR"):
        monkeypatch.delenv(name, raising=False)
    rendered = render_table(
        ("STATUS", "CHECK", "DETAILS"),
        (("OK", "config", "/tmp/config.toml"), ("FAIL", "model credentials", "missing")),
        _Pipe(),
    )
    lines = rendered.splitlines()
    assert len(lines) == 4
    assert "STATUS" in lines[0] and "CHECK" in lines[0] and "DETAILS" in lines[0]
    assert "config" in lines[2] and "/tmp/config.toml" in lines[2]
    assert "model credentials" in lines[3] and "missing" in lines[3]
    assert "\x1b[" not in rendered
    assert lines[2].index("config") == lines[0].index("CHECK")
    assert lines[2].index("/tmp/config.toml") == lines[0].index("DETAILS")
    assert lines[3].index("model credentials") == lines[0].index("CHECK")
    assert lines[3].index("missing") == lines[0].index("DETAILS")


def test_render_table_accepts_empty_cells() -> None:
    from src.cli_format import render_table

    rendered = render_table(("STATUS", "CHECK", "DETAILS"), (("", "config", ""),), _Pipe())

    lines = rendered.splitlines()
    assert lines[0].startswith("STATUS  CHECK") and "DETAILS" in lines[0]
    assert len(lines[1]) == len(lines[0])
    assert len(lines) == 3 and "config" in lines[2]


def test_render_table_wraps_multiline_details_at_narrow_width(monkeypatch) -> None:
    from types import SimpleNamespace

    import src.cli_format as formatting

    monkeypatch.setattr(formatting.shutil, "get_terminal_size", lambda: SimpleNamespace(columns=40))
    rendered = formatting.render_table(
        ("STATUS", "CHECK", "DETAILS"),
        (("OK", "probe", "first line\nsecond line has enough words to wrap"),),
        _Pipe(),
    )

    lines = rendered.splitlines()
    assert "first line" in lines[2]
    assert "second line" in lines[3]
    assert "wrap" in lines[-1]
    assert max(map(len, lines)) <= 40


def test_doctor_renders_a_table_and_preserves_json_contract(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checks = [
        Check("config", True, "/tmp/config.toml"),
        Check("model credentials", False, "environment variable is not set: API_KEY"),
    ]
    monkeypatch.setattr(cli, "doctor", lambda _path: checks)

    with pytest.raises(SystemExit) as error:
        cli.main(["doctor"])
    assert error.value.code == 1
    output = capsys.readouterr().out
    assert "STATUS" in output and "CHECK" in output and "DETAILS" in output
    assert "model credentials" in output
    assert "environment variable is not set: API_KEY" in output

    for name in ("NO_COLOR", "TERM", "FORCE_COLOR"):
        monkeypatch.delenv(name, raising=False)
    terminal = _TTY()
    monkeypatch.setattr(cli.sys, "stdout", terminal)
    with pytest.raises(SystemExit) as tty_error:
        cli.main(["doctor"])
    assert tty_error.value.code == 1
    assert "\x1b[32mOK\x1b[0m" in terminal.getvalue()
    assert "\x1b[31mFAIL\x1b[0m" in terminal.getvalue()

    before_json = len(terminal.getvalue())
    with pytest.raises(SystemExit) as json_error:
        cli.main(["doctor", "--json"])
    assert json_error.value.code == 1
    machine = terminal.getvalue()[before_json:]
    assert json.loads(machine) == {
        "checks": [
            {"ok": True, "name": "config", "message": "/tmp/config.toml"},
            {
                "ok": False,
                "name": "model credentials",
                "message": "environment variable is not set: API_KEY",
            },
        ]
    }


@pytest.mark.parametrize("active_error", [None, "bundle catalog unavailable"])
def test_status_json_reports_active_bundle(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    active_error: str | None,
) -> None:
    monkeypatch.setattr(cli, "doctor", lambda _path: [Check("config", True, "ok")])
    monkeypatch.setattr(cli, "load_config", lambda *_args, **_kwargs: object())
    if active_error is None:
        monkeypatch.setattr(cli, "load_active_bundle", lambda _config: None)
    else:
        def fail_to_load(_config):
            raise RuntimeError(active_error)

        monkeypatch.setattr(cli, "load_active_bundle", fail_to_load)

    cli.main(["status", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"] == [{"ok": True, "name": "config", "message": "ok"}]
    assert payload["active_bundle"] == ("none" if active_error is None else "unavailable")
    if active_error is not None:
        assert payload["active_bundle_error"] == active_error
