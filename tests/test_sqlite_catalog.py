from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src import sqlite_store
from src.models import Incident, TaskEvent, TaskRecord, TaskState
from src.sqlite_store import connect, transaction
from src.task_catalog import TaskCatalog


def _incident() -> Incident:
    return Incident(
        external_id="incident-1",
        source="test",
        repository="org/service",
        environment="prod",
        summary="failure",
    )


def _task() -> TaskRecord:
    return TaskRecord(
        external_id="incident-1",
        source="test",
        repository="org/service",
        environment="prod",
        summary="failure",
        conversation_id="conversation-1",
    )


def test_transaction_rolls_back_and_configures_database(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    with pytest.raises(RuntimeError), transaction(path) as db:
        db.execute("CREATE TABLE values_table (value INTEGER)")
        db.execute("INSERT INTO values_table VALUES (1)")
        raise RuntimeError("abort")
    with connect(path) as db:
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='values_table'").fetchone()
            is None
        )
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert db.execute("SELECT name FROM runtime_migrations").fetchone() is None


def test_wal_reader_sees_last_commit_during_writer(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    with transaction(path) as db:
        db.execute("CREATE TABLE values_table (value INTEGER)")
        db.execute("INSERT INTO values_table VALUES (1)")
    entered = threading.Event()
    release = threading.Event()

    def writer() -> None:
        with transaction(path) as db:
            db.execute("INSERT INTO values_table VALUES (2)")
            entered.set()
            release.wait(5)

    thread = threading.Thread(target=writer)
    thread.start()
    assert entered.wait(5)
    with connect(path) as db:
        assert [row[0] for row in db.execute("SELECT value FROM values_table")] == [1]
    release.set()
    thread.join(5)
    assert not thread.is_alive()


def test_process_exit_before_commit_rolls_back_and_preserves_integrity(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    with connect(path) as db:
        db.execute("CREATE TABLE values_table (value INTEGER)")
    code = (
        "from src.sqlite_store import transaction; "
        f"db=transaction({str(path)!r}); c=db.__enter__(); "
        "c.execute('INSERT INTO values_table VALUES (9)'); __import__('os')._exit(0)"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=Path.cwd())
    assert result.returncode == 0
    with connect(path) as db:
        assert db.execute("SELECT * FROM values_table").fetchall() == []
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_catalog_deduplicates_concurrent_create_and_leases(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    incident = _incident()

    def create(_: int) -> str:
        catalog = TaskCatalog(path)
        task, _ = catalog.create(_task(), incident)
        return task.task_id

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(create, range(16)))
    assert len(set(ids)) == 1
    catalog = TaskCatalog(path)
    assert catalog.acquire(ids[0], "worker-a", 60)
    assert not catalog.acquire(ids[0], "worker-b", 60)


def test_json_import_has_priority_and_marker_blocks_late_legacy(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    task, incident = _task(), _incident()
    document = {"version": 1, "tasks": {}, "events": {}, "workspaces": {}, "leases": {}}
    path.write_text(json.dumps(document), encoding="utf-8")
    catalog = TaskCatalog(path)
    assert catalog.tasks() == []
    legacy = tmp_path / "tasks.sqlite3"
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
    restarted = TaskCatalog(path)
    assert restarted.tasks() == []
    assert legacy.exists()


def test_populated_json_import_restores_all_catalog_state_and_preserves_source(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tasks.json"
    task, incident = _task(), _incident()
    event = TaskEvent(type="context.collected")
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    stat = workspace.stat()
    document = {
        "version": 1,
        "tasks": {
            task.task_id: {
                "scope": TaskCatalog.scope(incident),
                "state": task.state.value,
                "record": task.model_dump(mode="json"),
                "incident": incident.model_dump(mode="json"),
            }
        },
        "events": {task.task_id: [event.model_dump(mode="json")]},
        "workspaces": {
            task.task_id: {
                "root": str(workspace),
                "device": stat.st_dev,
                "inode": stat.st_ino,
                "active": 1,
            }
        },
        "leases": {task.task_id: {"owner": "worker", "expires": 9999999999.0}},
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    source = path.read_bytes()
    catalog = TaskCatalog(path)
    assert catalog.load(task.task_id) == task
    assert catalog.incident(task.task_id) == incident
    assert catalog.events(task.task_id)[0] == event
    assert not catalog.acquire(task.task_id, "other", 1)
    catalog.verify_workspace(task.task_id, workspace)
    assert path.read_bytes() == source


def test_duplicate_json_scope_rolls_back_migration(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    first, second = _task(), _task()
    incident = _incident()
    records = {}
    for task in (first, second):
        records[task.task_id] = {
            "scope": TaskCatalog.scope(incident),
            "state": task.state.value,
            "record": task.model_dump(mode="json"),
            "incident": incident.model_dump(mode="json"),
        }
    path.write_text(
        json.dumps({"version": 1, "tasks": records, "events": {}, "workspaces": {}, "leases": {}}),
        encoding="utf-8",
    )
    with pytest.raises(sqlite3.IntegrityError):
        TaskCatalog(path)
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM catalog_tasks").fetchone()[0] == 0


def test_corrupt_json_fails_closed_without_catalog_rows(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot migrate task catalog"):
        TaskCatalog(path)
    if (tmp_path / "runtime.sqlite3").exists():
        with sqlite3.connect(tmp_path / "runtime.sqlite3") as db:
            assert db.execute("SELECT COUNT(*) FROM catalog_tasks").fetchone()[0] == 0


@pytest.mark.parametrize(
    "field, value",
    [
        ("version", 2),
        ("events", {"missing": {}}),
        ("workspaces", {"missing": {}}),
        ("leases", {"missing": {}}),
    ],
)
def test_malformed_json_catalog_fails_closed(tmp_path: Path, field: str, value: object) -> None:
    path = tmp_path / "tasks.json"
    document: dict[str, object] = {
        "version": 1,
        "tasks": {},
        "events": {},
        "workspaces": {},
        "leases": {},
    }
    document[field] = value
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError):
        TaskCatalog(path)


def test_catalog_validation_and_atomic_events(tmp_path: Path) -> None:
    catalog = TaskCatalog(tmp_path / "tasks.json")
    task, incident = _task(), _incident()
    catalog.create(task, incident)
    event = TaskEvent(type="task.saved")
    catalog.save(task, event)
    assert catalog.events(task.task_id)[-1] == event
    with pytest.raises(FileNotFoundError):
        catalog.save(task.model_copy(update={"task_id": "missing"}))
    with pytest.raises(FileNotFoundError):
        catalog.transition("missing", task.state)
    with pytest.raises(ValueError, match="unknown task field"):
        catalog.transition(task.task_id, TaskState.COLLECTING_CONTEXT, nope=True)


def test_prepare_closes_connection_after_non_lock_error(monkeypatch, tmp_path: Path) -> None:
    class Broken:
        def execute(self, statement: str):
            if "busy_timeout" in statement:
                return self
            raise sqlite3.OperationalError("permission denied")

        def close(self):
            self.closed = True

    broken = Broken()
    monkeypatch.setattr(sqlite_store.sqlite3, "connect", lambda *_args, **_kwargs: broken)
    with pytest.raises(sqlite3.OperationalError, match="permission"):
        sqlite_store._prepare(tmp_path / "runtime.sqlite3")
    assert broken.closed


def test_prepare_retries_locked_wal(monkeypatch, tmp_path: Path) -> None:
    class Flaky:
        def __init__(self):
            self.calls = 0
            self.closed = False

        def execute(self, statement: str):
            if "journal_mode" in statement:
                self.calls += 1
                if self.calls == 1:
                    raise sqlite3.OperationalError("database is locked")
            return self

        def close(self):
            self.closed = True

    flaky = Flaky()
    monkeypatch.setattr(sqlite_store.sqlite3, "connect", lambda *_args, **_kwargs: flaky)
    assert sqlite_store._prepare(tmp_path / "runtime.sqlite3") is flaky
    assert flaky.calls == 2
