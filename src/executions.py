"""List and inspect durable agent runs stored for incident tasks."""

from __future__ import annotations

import json
import shutil
import sys
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.theme import Theme
from textual.widgets import DataTable, Footer, Header, Static

from .config import load_config
from .file_session import FileSession
from .models import TaskRecord
from .storage import Storage

_CHILD_SESSIONS = (":research", ":implementation")
_TOOL_OUTPUT_LIMIT = 8_000


def session_id_for(task: TaskRecord) -> str:
    """Return the durable lead-session id the agent uses for a task."""
    return task.agent_session_id or f"task:{task.task_id}"


def list_executions(storage: Storage) -> list[TaskRecord]:
    """Return every task session, newest activity first."""
    return sorted(
        storage.list_tasks(),
        key=lambda task: (task.updated_at, task.created_at, task.task_id),
        reverse=True,
    )


def find_execution(storage: Storage, identifier: str) -> TaskRecord:
    """Resolve a task by the agent's session id, task id, or conversation id."""
    needle = identifier.strip()
    if not needle:
        raise KeyError("missing agent session id")
    parent = needle
    for suffix in _CHILD_SESSIONS:
        if needle.endswith(suffix):
            parent = needle[: -len(suffix)]
            break
    matches = [
        task
        for task in storage.list_tasks()
        if parent in {task.task_id, session_id_for(task), task.conversation_id}
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise KeyError(f"unknown agent session: {needle}")
    raise KeyError(f"ambiguous agent session: {needle}")


def format_execution_list(tasks: list[TaskRecord], width: int | None = None) -> str:
    """Render session id, summary title, and a right-aligned date."""
    columns = width or shutil.get_terminal_size(fallback=(80, 24)).columns
    columns = max(40, columns)
    lines: list[str] = []
    for task in tasks:
        ident = session_id_for(task)
        title = " ".join((task.summary or "").split()) or "(no summary)"
        date = _date(task.updated_at or task.created_at)
        left = f"{ident}  {title}"
        room = max(8, columns - len(date) - 2)
        if len(left) > room:
            left = left[: room - 1].rstrip() + "…"
        lines.append(f"{left:<{columns - len(date)}}{date}")
    return "\n".join(lines)


def execution_transcript(storage: Storage, task: TaskRecord) -> str:
    """Render the stored model conversation for a task session."""
    ident = session_id_for(task)
    header = [
        f"session: {ident}",
        f"task: {task.task_id}",
        f"summary: {' '.join((task.summary or '').split()) or '(no summary)'}",
        f"state: {task.state.value}",
        f"updated: {_date(task.updated_at or task.created_at)} UTC",
        "",
    ]
    sections: list[str] = []
    lead = _session_items(storage, ident)
    if lead:
        sections.append(_render_items(lead))
    for suffix, label in ((":research", "Research"), (":implementation", "Implementation")):
        items = _session_items(storage, ident + suffix)
        if items:
            sections.append(f"## {label}\n{_render_items(items)}")
    if not sections:
        messages = storage.messages(task.conversation_id)
        if messages:
            sections.append(
                "\n\n".join(f"{role}: {content}" for role, content in messages if content)
            )
    body = "\n\n".join(section for section in sections if section).strip()
    if not body:
        body = "no model conversation stored for this session"
    return "\n".join(header) + body + "\n"


def run_executions_command(args: Namespace) -> None:
    """Dispatch ``incident-agent executions`` list and inspect."""
    target = str(getattr(args, "target", "list") or "list")
    action = getattr(args, "action", None)
    if target == "list":
        if action:
            raise SystemExit("executions list does not take inspect")
        _show_list(_storage_for(args.config))
        return
    storage = _storage_for(args.config)
    try:
        task = find_execution(storage, target)
    except KeyError as error:
        raise SystemExit(str(error)) from error
    transcript = execution_transcript(storage, task)
    if _tty():
        ExecutionInspectApp(session_id_for(task), task.summary, transcript).run()
        return
    print(transcript.rstrip("\n"))


_GRAPHITE = Theme(
    name="incident-graphite",
    primary="#66d9c3",
    secondary="#a8b8c4",
    accent="#e9b96e",
    foreground="#e4edf2",
    background="#10181f",
    surface="#17232c",
    panel="#20313b",
    success="#66d9a0",
    warning="#e9b96e",
    error="#ff8c82",
    dark=True,
)
_EXECUTIONS_CSS = """
Screen { background: $background; }
Header { background: $panel; color: $text; }
HeaderIcon { display: none; }
Footer { background: $panel; }
#transcript { padding: 1 2; height: auto; color: $text; }
#cli-hint { height: auto; color: $text-muted; padding: 0 2 1 2; }
#executions { height: 1fr; }
#empty { height: 1fr; padding: 1 2; color: $text-muted; content-align: center middle; }
DataTable { height: 1fr; }
"""


class ExecutionInspectScreen(Screen[None]):
    """Conversation overlay opened from the executions list."""

    BINDINGS = [
        Binding("q", "back", "Back"),
        Binding("escape", "back", "Back"),
    ]

    def __init__(self, session_id: str, summary: str, transcript: str) -> None:
        super().__init__()
        self._title = summary or session_id
        self._transcript = transcript
        self._previous_sub_title = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(Static(self._transcript, markup=False, id="transcript"))
        yield Footer()

    def on_mount(self) -> None:
        self._previous_sub_title = str(self.app.sub_title or "")
        self.app.sub_title = self._title

    def on_unmount(self) -> None:
        self.app.sub_title = self._previous_sub_title

    def action_back(self) -> None:
        self.app.pop_screen()


class ExecutionInspectApp(App[None]):
    """Read-only viewer for a stored agent conversation."""

    TITLE = "Incident Agent"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("escape", "quit", "Quit"),
    ]
    CSS = _EXECUTIONS_CSS

    def __init__(self, session_id: str, summary: str, transcript: str) -> None:
        super().__init__()
        self.register_theme(_GRAPHITE)
        self.theme = "incident-graphite"
        self.sub_title = summary or session_id
        self._transcript = transcript
        self._session_id = session_id

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            f"q quits · inspect from CLI: incident-agent executions {self._session_id} inspect",
            id="cli-hint",
            markup=False,
        )
        yield VerticalScroll(Static(self._transcript, markup=False, id="transcript"))
        yield Footer()


class ExecutionListApp(App[None]):
    """Browse previous agent runs and inspect a stored conversation."""

    TITLE = "Incident Agent"
    SUB_TITLE = "Executions"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("escape", "quit", "Quit"),
    ]
    CSS = _EXECUTIONS_CSS

    def __init__(self, storage: Storage, tasks: list[TaskRecord]) -> None:
        super().__init__()
        self.register_theme(_GRAPHITE)
        self.theme = "incident-graphite"
        self._storage = storage
        self._tasks = tasks

    def compose(self) -> ComposeResult:
        yield Header()
        if self._tasks:
            yield DataTable(
                id="executions",
                cursor_type="row",
                zebra_stripes=True,
                show_row_labels=False,
            )
        else:
            yield Static("no agent runs", id="empty", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        if not self._tasks:
            return
        table = self.query_one("#executions", DataTable)
        table.add_column("Summary", key="summary")
        table.add_column("Date", key="date", width=16)
        for index, task in enumerate(self._tasks):
            title = " ".join((task.summary or "").split()) or "(no summary)"
            date = _date(task.updated_at or task.created_at)
            table.add_row(title, Text(date, justify="right"), key=str(index))
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.open_row(event.cursor_row)

    def open_row(self, index: int) -> None:
        if index < 0 or index >= len(self._tasks):
            return
        task = self._tasks[index]
        self.push_screen(
            ExecutionInspectScreen(
                session_id_for(task),
                task.summary,
                execution_transcript(self._storage, task),
            )
        )


def _storage_for(config_path: Path) -> Storage:
    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"configuration file is missing: {path}")
    try:
        config = load_config(path, create=False)
    except (OSError, ValueError) as error:
        raise SystemExit(f"could not read configuration: {error}") from error
    root = Path(config.runtime_root).expanduser()
    resolved = (Path.cwd() / root if not root.is_absolute() else root).resolve()
    return Storage(resolved)


def _tty() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def _show_list(storage: Storage) -> None:
    tasks = list_executions(storage)
    if _tty():
        ExecutionListApp(storage, tasks).run()
        return
    if not tasks:
        print("no agent runs")
        return
    print(format_execution_list(tasks))


def _date(value: datetime) -> str:
    when = value if value.tzinfo else value.replace(tzinfo=UTC)
    return when.astimezone(UTC).strftime("%Y-%m-%d %H:%M")


def _session_items(storage: Storage, session_id: str) -> list[Any]:
    return FileSession(session_id, db_path=storage.runtime_db).load_items()


def _render_items(items: list[Any]) -> str:
    lines = [rendered for item in items if (rendered := _render_item(item))]
    return "\n\n".join(lines)


def _render_item(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    role = item.get("role")
    item_type = str(item.get("type") or "")
    content = _content_text(item.get("content"))
    if role:
        extra = content
        name = item.get("name")
        if name and not extra:
            extra = _content_text(item.get("arguments"))
        if name:
            extra = f"{name}: {extra}" if extra else str(name)
        return f"{role}: {extra}".rstrip()
    if "function_call_output" in item_type or item_type in {"tool_output", "hosted_tool_output"}:
        output = item.get("output")
        if output is None:
            output = item.get("content")
        return f"tool output: {_clip(_content_text(output), _TOOL_OUTPUT_LIMIT)}"
    if "function_call" in item_type or item_type in {"tool_call", "hosted_tool_call"}:
        name = item.get("name") or "tool"
        arguments = item.get("arguments") or ""
        if isinstance(arguments, (dict, list)):
            arguments = json.dumps(arguments, ensure_ascii=False)
        return f"tool {name}: {_clip(str(arguments), _TOOL_OUTPUT_LIMIT)}"
    if "reasoning" in item_type:
        thinking = _content_text(item.get("summary") or content)
        return f"thinking: {_clip(thinking, _TOOL_OUTPUT_LIMIT)}" if thinking else ""
    if content:
        return content
    return json.dumps(item, ensure_ascii=False) if not item_type else ""


def _content_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_content_text(part) for part in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        if "text" in value or "content" in value:
            return _content_text(value.get("text") or value.get("content"))
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
