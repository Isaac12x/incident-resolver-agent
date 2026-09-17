from __future__ import annotations

import json
import multiprocessing
import sqlite3
from pathlib import Path

import pytest

from src.file_store import JsonFile
from src.models import Incident, TaskEvent, TaskRecord, TaskState
from src.storage import Storage
from src.task_catalog import TaskCatalog


def _increment(path: str, count: int) -> None:
    store = JsonFile(Path(path), lambda: {"value": 0})
    for _ in range(count):
        with store.transaction() as data:
            data["value"] += 1


def test_transaction_persists_and_restart_reads(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = JsonFile(path, lambda: {"value": 0})
    with store.transaction() as data:
        data["value"] = 7
    assert JsonFile(path, lambda: {}).read() == {"value": 7}
    assert list(tmp_path.glob(".state.json.*")) == []


def test_concurrent_transactions_do_not_lose_updates(tmp_path: Path) -> None:
    path = tmp_path / "counter.json"
    processes = [multiprocessing.Process(target=_increment, args=(str(path), 20)) for _ in range(4)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert JsonFile(path, lambda: {}).read()["value"] == 80


def test_corruption_is_visible(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        JsonFile(path, dict).read()


def test_legacy_session_tables_migrate_and_source_is_unchanged(tmp_path: Path) -> None:
    legacy = tmp_path / "sessions.sqlite3"
    with sqlite3.connect(legacy) as db:
        db.execute("CREATE TABLE messages (conversation_id, role, content, created_at)")
        db.execute(
            "CREATE TABLE observability_events (event_id, source, group_key, "
            "fingerprint, payload, received_at, duplicate_of, task_id)"
        )
        db.execute(
            "CREATE TABLE incident_history (task_id, external_id, source, repository, "
            "environment, summary, description, root_cause, outcome, created_at)"
        )
        db.execute("INSERT INTO messages VALUES ('c', 'user', 'hello', '2025-01-01')")
        db.execute(
            "INSERT INTO observability_events VALUES "
            "('e', 's', 'g', 'f', '{\"x\": 1}', '2025-01-01', NULL, NULL)"
        )
        db.execute(
            "INSERT INTO incident_history VALUES "
            "('t', 'x', 's', 'r', 'prod', 'sum', 'desc', 'cause', 'done', '2025-01-01')"
        )
    before = legacy.read_bytes()
    storage = Storage(tmp_path)
    assert storage.messages("c") == [("user", "hello")]
    assert storage.list_observability_events()[0]["payload"] == {"x": 1}
    assert storage.incident_history()[0]["task_id"] == "t"
    assert legacy.read_bytes() == before
    with storage.messages_store.transaction() as data:
        data["messages"].append(
            {
                "conversation_id": "c",
                "role": "assistant",
                "content": "new",
                "created_at": "2025-01-02",
            }
        )
    restarted = Storage(tmp_path)
    assert restarted.messages("c")[-1] == ("assistant", "new")


def test_legacy_partial_tables_create_empty_authoritative_files(tmp_path: Path) -> None:
    legacy = tmp_path / "sessions.sqlite3"
    with sqlite3.connect(legacy) as db:
        db.execute("CREATE TABLE messages (conversation_id, role, content, created_at)")
    storage = Storage(tmp_path)
    assert storage.messages_store.path.exists()
    assert storage.events_store.path.exists()
    assert storage.messages("missing") == []


def test_corrupt_legacy_sessions_fail_loudly(tmp_path: Path) -> None:
    (tmp_path / "sessions.sqlite3").write_bytes(b"not sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        Storage(tmp_path)


def test_legacy_catalog_sql_migrates_all_state_and_preserves_source(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    legacy = root / "tasks.sqlite3"
    task = TaskRecord(
        task_id="TASK-LEGACY",
        external_id="ext-1",
        source="pagerduty",
        repository="org/service",
        environment="prod",
        summary="broken",
        conversation_id="conv-1",
        state=TaskState.INVESTIGATING,
    )
    incident = Incident(
        external_id="ext-1",
        source="pagerduty",
        repository="org/service",
        environment="prod",
        summary="broken",
    )
    event = TaskEvent(type="context.collected")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    stat = worktree.stat()
    with sqlite3.connect(legacy) as db:
        db.executescript(
            "CREATE TABLE catalog_tasks (task_id TEXT PRIMARY KEY, scope TEXT UNIQUE, "
            "state TEXT, record TEXT, incident TEXT);"
            "CREATE TABLE catalog_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
            "task_id TEXT, event TEXT);"
            "CREATE TABLE catalog_workspaces (task_id TEXT PRIMARY KEY, root TEXT, "
            "device INTEGER, inode INTEGER, active INTEGER);"
            "CREATE TABLE catalog_leases (task_id TEXT PRIMARY KEY, owner TEXT, expires REAL);"
        )
        db.execute(
            "INSERT INTO catalog_tasks VALUES (?, ?, ?, ?, ?)",
            (
                task.task_id,
                TaskCatalog.scope(incident),
                task.state.value,
                task.model_dump_json(),
                incident.model_dump_json(),
            ),
        )
        db.execute(
            "INSERT INTO catalog_events(task_id, event) VALUES (?, ?)",
            (task.task_id, event.model_dump_json()),
        )
        db.execute(
            "INSERT INTO catalog_workspaces VALUES (?, ?, ?, ?, 1)",
            (task.task_id, str(worktree), stat.st_dev, stat.st_ino),
        )
        db.execute(
            "INSERT INTO catalog_leases VALUES (?, ?, ?)", (task.task_id, "worker", 9999999999.0)
        )
    before = legacy.read_bytes()
    catalog = TaskCatalog(root / "tasks.json")
    assert catalog.load(task.task_id).state == TaskState.INVESTIGATING
    assert catalog.events(task.task_id)[0].type == event.type
    assert catalog.acquire(task.task_id, "other", 1) is False
    catalog.verify_workspace(task.task_id, worktree)
    assert legacy.read_bytes() == before
    (root / "tasks.json").write_text(
        json.dumps({"version": 1, "tasks": {}, "events": {}, "workspaces": {}, "leases": {}})
    )
    restarted = TaskCatalog(root / "tasks.json")
    assert restarted.tasks() == []


def test_corrupt_legacy_catalog_fails_loudly(tmp_path: Path) -> None:
    (tmp_path / "tasks.sqlite3").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="cannot migrate"):
        TaskCatalog(tmp_path / "tasks.json")


def test_json_transaction_exception_rolls_back(tmp_path: Path) -> None:
    store = JsonFile(tmp_path / "state.json", lambda: {"value": 1})
    with store.transaction() as data:
        data["value"] = 2
    with pytest.raises(RuntimeError), store.transaction() as data:
        data["value"] = 3
        raise RuntimeError("abort")
    assert store.read()["value"] == 2


def test_json_empty_transaction_initializes_store(tmp_path: Path) -> None:
    path = tmp_path / "new.json"
    store = JsonFile(path, lambda: {"value": 0})
    with store.transaction():
        pass
    assert json.loads(path.read_text()) == {"value": 0}


def test_json_invalid_root_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "list.json"
    path.write_text("[]")
    with pytest.raises(ValueError, match="root"):
        JsonFile(path, dict).read()


def test_json_replace_failure_preserves_commit_and_cleans_temp(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "state.json"
    store = JsonFile(path, lambda: {"value": 1})
    with store.transaction() as data:
        data["value"] = 1

    def fail_replace(self, target):
        raise OSError("injected replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"), store.transaction() as data:
        data["value"] = 2
    assert json.loads(path.read_text()) == {"value": 1}
    assert list(tmp_path.glob(".state.json.*")) == []
