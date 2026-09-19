"""CLI listing and inspection of durable agent sessions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.widgets import DataTable, Static

from src import __main__ as cli
from src.config import Config, save_config
from src.executions import (
    ExecutionInspectApp,
    ExecutionListApp,
    _clip,
    _date,
    _render_item,
    _style_transcript,
    execution_transcript,
    find_execution,
    format_execution_list,
    list_executions,
    session_id_for,
)
from src.file_session import FileSession
from src.models import Incident, TaskRecord
from src.storage import Storage
from src.tui_theme import GROKNIGHT


def _setup(tmp_path: Path) -> tuple[Path, Storage]:
    runtime = tmp_path / "runtime"
    config_path = tmp_path / "config.toml"
    save_config(Config(runtime_root=runtime), config_path)
    return config_path, Storage(runtime)


def _incident(external_id: str, summary: str) -> Incident:
    return Incident(
        external_id=external_id,
        source="test",
        repository="org/service",
        environment="prod",
        summary=summary,
    )


def test_parse_executions_list_and_session_actions() -> None:
    listed = cli.parse_arguments(["executions"])
    assert listed.command == "executions"
    assert listed.target == "list"
    assert listed.action is None
    inspect = cli.parse_arguments(["executions", "task:ABC", "inspect"])
    assert inspect.target == "task:ABC"
    assert inspect.action == "inspect"
    with pytest.raises(SystemExit):
        cli.parse_arguments(["executions", "task:ABC", "jack-in"])


def test_list_prints_session_id_summary_and_right_aligned_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, storage = _setup(tmp_path)
    older = storage.create_task(_incident("old", "Older checkout failure"))
    newer = storage.create_task(_incident("new", "Payment 500 in production"))
    monkeypatch.setattr(
        "src.executions.shutil.get_terminal_size",
        lambda fallback=(80, 24): __import__("os").terminal_size((80, 24)),
    )
    cli.main(["--config", str(config_path), "executions", "list"])
    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert len(lines) == 2
    assert lines[0].startswith(session_id_for(newer))
    assert "Payment 500 in production" in lines[0]
    assert lines[1].startswith(session_id_for(older))
    date = (newer.updated_at or newer.created_at).strftime("%Y-%m-%d %H:%M")
    assert lines[0].endswith(date)
    assert lines[0].index("Payment") < lines[0].rindex(date)
    assert lines[0][len(lines[0]) - len(date) :] == date


def test_empty_list_and_truncated_title(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path, storage = _setup(tmp_path)
    cli.main(["--config", str(config_path), "executions"])
    assert capsys.readouterr().out.strip() == "no agent runs"
    task = storage.create_task(
        _incident("long", "A" * 80),
    )
    rendered = format_execution_list([task], width=50)
    date = (task.updated_at or task.created_at).strftime("%Y-%m-%d %H:%M")
    assert rendered.endswith(date)
    assert "…" in rendered
    assert session_id_for(task) in rendered


def test_inspect_prints_sdk_conversation_and_accepts_task_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path, storage = _setup(tmp_path)
    task = storage.create_task(_incident("one", "Checkout returns 500"))
    asyncio.run(
        FileSession(session_id_for(task), db_path=storage.runtime_db).add_items(
            [
                {"role": "user", "content": "fix the checkout 500"},
                {"role": "assistant", "content": "the handler dropped the session cookie"},
                {
                    "type": "function_call",
                    "name": "read_file",
                    "arguments": {"path": "app.py"},
                },
                {"type": "function_call_output", "output": "def handle():\n    pass"},
            ]
        )
    )
    asyncio.run(
        FileSession(session_id_for(task) + ":research", db_path=storage.runtime_db).add_items(
            [{"role": "assistant", "content": "root cause is missing cookie"}]
        )
    )
    asyncio.run(
        FileSession(
            session_id_for(task) + ":implementation", db_path=storage.runtime_db
        ).add_items([{"role": "assistant", "content": "patched the cookie guard"}])
    )
    cli.main(["--config", str(config_path), "executions", task.task_id, "inspect"])
    output = capsys.readouterr().out
    assert f"session: {session_id_for(task)}" in output
    assert "Checkout returns 500" in output
    assert "user: fix the checkout 500" in output
    assert "assistant: the handler dropped the session cookie" in output
    assert "tool read_file:" in output
    assert "tool output:" in output
    assert "## Research" in output
    assert "root cause is missing cookie" in output
    assert "## Implementation" in output
    assert "patched the cookie guard" in output


def test_inspect_falls_back_to_messages_and_child_session_lookup(tmp_path: Path) -> None:
    _, storage = _setup(tmp_path)
    task = storage.create_task(_incident("two", "Loki timeout"))
    storage.add_message(task.conversation_id, "user", "investigate: timeout")
    storage.add_message(task.conversation_id, "assistant", "query window too large")
    found = find_execution(storage, session_id_for(task) + ":implementation")
    assert found.task_id == task.task_id
    assert find_execution(storage, task.conversation_id).task_id == task.task_id
    transcript = execution_transcript(storage, task)
    assert "user: investigate: timeout" in transcript
    assert "assistant: query window too large" in transcript


def test_inspect_empty_session_and_unknown_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path, storage = _setup(tmp_path)
    task = storage.create_task(_incident("empty", "Blank title later"))
    task.summary = ""
    storage.catalog.save(task)
    cli.main(["--config", str(config_path), "executions", session_id_for(task), "inspect"])
    output = capsys.readouterr().out
    assert "(no summary)" in output
    assert "no model conversation stored for this session" in output
    with pytest.raises(SystemExit, match="unknown agent session"):
        cli.main(["--config", str(config_path), "executions", "task:missing", "inspect"])
    with pytest.raises(KeyError, match="missing agent session"):
        find_execution(storage, "  ")


def test_list_rejects_inspect_action(
    tmp_path: Path,
) -> None:
    config_path, _storage = _setup(tmp_path)
    with pytest.raises(SystemExit, match="does not take"):
        cli.main(["--config", str(config_path), "executions", "list", "inspect"])


def test_missing_config_and_relative_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit, match="configuration file is missing"):
        cli.main(["--config", str(tmp_path / "missing.toml"), "executions", "list"])
    monkeypatch.chdir(tmp_path)
    save_config(Config(runtime_root=Path("agent-state")), tmp_path / "config.toml")
    cli.main(["--config", str(tmp_path / "config.toml"), "executions", "list"])
    assert capsys.readouterr().out.strip() == "no agent runs"
    assert (tmp_path / "agent-state").is_dir()


def test_invalid_config_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("runtime_root = [\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="could not read configuration"):
        cli.main(["--config", str(path), "executions", "list"])


def test_inspect_session_id_without_action_prints(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path, storage = _setup(tmp_path)
    task = storage.create_task(_incident("bare", "Inspect me"))
    storage.add_message(task.conversation_id, "assistant", "cookie missing")
    cli.main(["--config", str(config_path), "executions", session_id_for(task)])
    assert "cookie missing" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_inspect_app_renders_transcript() -> None:
    app = ExecutionInspectApp("task:ABC", "", "assistant: hello from the model")
    async with app.run_test():
        assert app.theme == GROKNIGHT.name
        assert app.sub_title == "task:ABC"
        transcript = app.query_one("#transcript", Static).render().plain
        assert "hello from the model" in transcript
        assert "◆" in transcript
        assert "incident-agent executions task:ABC inspect" in app.query_one(
            "#cli-hint", Static
        ).render().plain


def test_ambiguous_session_and_item_rendering(tmp_path: Path) -> None:
    _, storage = _setup(tmp_path)
    first = TaskRecord(
        task_id="ONE",
        external_id="a",
        source="test",
        repository="org/service",
        environment="prod",
        summary="one",
        conversation_id="c1",
        agent_session_id="task:shared",
    )
    second = first.model_copy(
        update={"task_id": "TWO", "external_id": "b", "conversation_id": "c2"}
    )
    storage.list_tasks = lambda *buckets: [first, second]  # type: ignore[method-assign]
    with pytest.raises(KeyError, match="ambiguous"):
        find_execution(storage, "task:shared")
    assert _render_item("plain") == "plain"
    assert _render_item({"role": "assistant", "name": "delegate", "content": "ok"}) == (
        "assistant: delegate: ok"
    )
    assert "tool shell:" in _render_item(
        {"type": "hosted_tool_call", "name": "shell", "arguments": "ls"}
    )
    assert _render_item({"type": "tool_output", "content": "done"}).startswith("tool output:")
    assert _render_item({"type": "reasoning", "summary": [{"text": "think"}]}).startswith(
        "thinking:"
    )
    assert _render_item({"type": "reasoning"}) == ""
    assert _render_item({"content": [{"text": "only"}]}) == "only"
    assert '"k"' in _render_item({"k": 1})
    assert '"k"' in _render_item({"content": {"k": 1}})
    assert _render_item({"content": 3}) == "3"
    assert _clip("x" * 10, 4).endswith("…")
    assert _clip("short", 10) == "short"
    assert _date(datetime(2026, 9, 18, 14, 32)) == "2026-09-18 14:32"
    assert _render_item({"role": "user", "name": "note"}) == "user: note"
    assert _render_item({"role": "assistant", "name": "fn", "arguments": "x"}) == (
        "assistant: fn: x"
    )
    assert _render_item({"type": "hosted_tool_output", "output": None, "content": "out"}) == (
        "tool output: out"
    )
    long_output = _render_item({"type": "function_call_output", "output": "z" * 9000})
    assert long_output.endswith("…")


def test_list_executions_orders_by_updated_at(tmp_path: Path) -> None:
    _, storage = _setup(tmp_path)
    first = storage.create_task(_incident("a", "First"))
    second = storage.create_task(_incident("b", "Second"))
    ordered = list_executions(storage)
    assert [task.task_id for task in ordered] == [second.task_id, first.task_id]
    first.updated_at = datetime(2030, 1, 1, tzinfo=UTC)
    storage.catalog.save(first)
    assert list_executions(storage)[0].task_id == first.task_id


class _TTY:
    def isatty(self) -> bool:
        return True


def test_tty_list_and_inspect_open_tui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, storage = _setup(tmp_path)
    task = storage.create_task(_incident("tui", "Payment 500 in production"))
    storage.add_message(task.conversation_id, "assistant", "cookie missing")
    monkeypatch.setattr("src.executions.sys.stdin", _TTY())
    monkeypatch.setattr("src.executions.sys.stdout", _TTY())
    listed: list[str] = []
    inspected: list[str] = []
    monkeypatch.setattr(
        "src.executions.ExecutionListApp.run",
        lambda self: listed.extend(item.summary for item in self._tasks),
    )
    monkeypatch.setattr(
        "src.executions.ExecutionInspectApp.run",
        lambda self: inspected.append(self._transcript),
    )
    cli.main(["--config", str(config_path), "executions", "list"])
    assert listed == ["Payment 500 in production"]
    cli.main(["--config", str(config_path), "executions", session_id_for(task), "inspect"])
    assert inspected and "cookie missing" in inspected[0]


@pytest.mark.asyncio
async def test_list_tui_shows_summary_date_and_inspects(tmp_path: Path) -> None:
    _, storage = _setup(tmp_path)
    older = storage.create_task(_incident("old", "Older checkout failure"))
    newer = storage.create_task(_incident("new", "Payment 500 in production"))
    storage.add_message(newer.conversation_id, "assistant", "the handler dropped the cookie")
    app = ExecutionListApp(storage, list_executions(storage))
    async with app.run_test() as pilot:
        table = app.query_one("#executions", DataTable)
        assert table.row_count == 2
        first = table.get_row_at(0)
        assert "Payment 500 in production" in str(first[0])
        date = (newer.updated_at or newer.created_at).strftime("%Y-%m-%d %H:%M")
        assert date in str(first[1])
        second = table.get_row_at(1)
        assert "Older checkout failure" in str(second[0])
        older_date = (older.updated_at or older.created_at).strftime("%Y-%m-%d %H:%M")
        assert older_date in str(second[1])
        table.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert "the handler dropped the cookie" in app.screen.query_one(
            "#transcript", Static
        ).render().plain
        assert app.sub_title == newer.summary
        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one("#executions", DataTable)
        assert app.sub_title == "Executions"
        app.open_row(-1)
        app.open_row(99)
    empty = ExecutionListApp(storage, [])
    async with empty.run_test():
        assert empty.theme == GROKNIGHT.name
        assert "no agent runs" in empty.query_one("#empty", Static).render().plain
        assert empty.query_one("#cli-hint", Static).render().plain == "q quit"


def test_style_transcript_roles() -> None:
    styled = _style_transcript(
        "\n".join(
            [
                "session: task:ABC",
                "task: ABC",
                "",
                "user: fix checkout",
                "",
                "assistant: cookie missing",
                "",
                "tool read_file: app.py",
                "",
                "tool output: def handle():",
                "    pass",
                "",
                "thinking: consider the cookie",
                "",
                "## Research",
                "root cause",
            ]
        )
    )
    plain = styled.plain
    assert "› " in plain
    assert "◆ " in plain
    assert "fix checkout" in plain
    assert "cookie missing" in plain
    assert "read_file: app.py" in plain
    assert "def handle():" in plain
    assert "consider the cookie" in plain
    assert "## Research" in plain
    assert any("#bb9af7" in str(span.style) for span in styled.spans)
    assert any("#6c6c6c" in str(span.style) for span in styled.spans)
