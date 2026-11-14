from __future__ import annotations

import pytest
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import DataTable, Input, Static, TabbedContent, TextArea

from src.config import Config, save_config
from src.executions import ExecutionInspectApp
from src.tui import ConfigurationApp
from src.tui_copy import copy_selection


@pytest.mark.asyncio
async def test_ctrl_c_copies_static_text_selection(tmp_path) -> None:
    path = tmp_path / "config.toml"
    save_config(Config(), path)
    app = ConfigurationApp(path)

    async with app.run_test(size=(100, 36)) as pilot:
        status = app.query_one("#status", Static)
        await pilot.mouse_down(status, offset=(1, 0))
        await pilot.hover(status, offset=(6, 0))
        await pilot.mouse_up(status, offset=(6, 0))
        await pilot.press("ctrl+c")
        await pilot.pause()

        assert app.clipboard == str(path)[:6]
        assert app.is_running


@pytest.mark.asyncio
async def test_ctrl_c_without_selection_does_not_quit(tmp_path) -> None:
    path = tmp_path / "config.toml"
    save_config(Config(), path)
    app = ConfigurationApp(path)

    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert app.is_running


@pytest.mark.asyncio
async def test_ctrl_c_copies_textarea_selection_and_keeps_editing(tmp_path) -> None:
    path = tmp_path / "config.toml"
    config = Config()
    config.agent.system_prompt = "copyable prompt"
    save_config(config, path)
    app = ConfigurationApp(path)

    async with app.run_test(size=(100, 36)) as pilot:
        app.query_one(TabbedContent).active = "safety-tab"
        await pilot.pause()
        prompt = app.query_one("#system-prompt", TextArea)
        prompt.selection = ((0, 0), (0, 8))
        prompt.focus()
        await pilot.press("ctrl+c")
        await pilot.pause()

        assert app.clipboard == "copyable"
        assert app.is_running


@pytest.mark.asyncio
async def test_ctrl_c_copies_input_selection(tmp_path) -> None:
    path = tmp_path / "config.toml"
    save_config(Config(), path)
    app = ConfigurationApp(path)

    async with app.run_test(size=(100, 36)) as pilot:
        field = app.query_one("#host", Input)
        field.value = "incident-host"
        field.focus()
        await pilot.pause()
        field.selection = (0, 8)
        assert field.selected_text == "incident"
        await pilot.press("ctrl+c")
        await pilot.pause()

        assert app.clipboard == "incident"
        assert app.is_running


@pytest.mark.asyncio
async def test_ctrl_c_copies_execution_transcript_selection() -> None:
    app = ExecutionInspectApp("session-1", "Example", "session: session-1\nTask body")

    async with app.run_test(size=(100, 30)) as pilot:
        transcript = app.query_one("#transcript", Static)
        app.screen.selections[transcript] = Selection(Offset(0, 0), Offset(9, 0))
        await pilot.press("ctrl+c")
        await pilot.pause()

        assert app.clipboard == "session: "
        assert app.is_running


@pytest.mark.asyncio
async def test_copy_selection_copies_focused_data_table_row() -> None:
    from textual.app import App, ComposeResult

    class TableApp(App[None]):
        def compose(self) -> ComposeResult:
            yield DataTable(id="table")

    app = TableApp()

    async with app.run_test():
        table = app.query_one("#table", DataTable)
        table.add_column("Session")
        table.add_column("Task")
        table.add_row("session-1", "Example task")
        table.move_cursor(row=0)
        table.focus()
        assert copy_selection(app) is True
        assert app.clipboard == "session-1\tExample task"



def test_copy_selection_without_selection_is_noop() -> None:
    class Screen:
        focused = None

        @staticmethod
        def get_selected_text():
            return None

    class AppStub:
        screen = Screen()
        clipboard = ""

        def copy_to_clipboard(self, text: str) -> None:
            self.clipboard = text

    app = AppStub()
    assert copy_selection(app) is False
    assert app.clipboard == ""
