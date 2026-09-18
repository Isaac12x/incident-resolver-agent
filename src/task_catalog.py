"""Durable SQLite-backed task catalog."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .lifecycle_graph import validate_transition
from .models import Incident, TaskEvent, TaskRecord, utc_now
from .sqlite_store import connect, transaction

_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_tasks (
 task_id TEXT PRIMARY KEY, scope TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
 record TEXT NOT NULL, incident TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog_events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 task_id TEXT NOT NULL REFERENCES catalog_tasks(task_id), event TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS catalog_events_task_idx ON catalog_events(task_id, sequence);
CREATE TABLE IF NOT EXISTS catalog_workspaces (
 task_id TEXT NOT NULL REFERENCES catalog_tasks(task_id),
 repository TEXT NOT NULL DEFAULT '',
 root TEXT NOT NULL, device INTEGER NOT NULL,
 inode INTEGER NOT NULL, active INTEGER NOT NULL,
 PRIMARY KEY(task_id, repository)
);
CREATE TABLE IF NOT EXISTS catalog_leases (
 task_id TEXT PRIMARY KEY REFERENCES catalog_tasks(task_id),
 owner TEXT NOT NULL, expires REAL NOT NULL
);
"""
_MIGRATION = "task_catalog:v1"


class TaskCatalog:
    def __init__(self, path: Path) -> None:
        requested = Path(path)
        self.path = (
            requested
            if requested.name == "runtime.sqlite3"
            else requested.with_name("runtime.sqlite3")
        )
        self.json_path = (
            requested if requested.suffix == ".json" else requested.with_name("tasks.json")
        )
        self.legacy_path = self.path.with_name("tasks.sqlite3")
        # ``executescript`` commits any open transaction before running its
        # statements.  Keep schema creation separate so a workspace identity
        # upgrade below always runs inside a transaction that can roll back as
        # one unit after a process interruption.
        with connect(self.path) as db:
            db.executescript(_SCHEMA)
        with transaction(self.path) as db:
            self._ensure_workspace_schema(db)
        self._migrate_json()
        self._migrate_legacy()

    @staticmethod
    def legacy_scope(incident: Incident) -> str:
        return json.dumps(
            [
                incident.source,
                incident.external_id,
                incident.repository.casefold(),
                incident.environment,
            ]
        )

    @classmethod
    def scope(cls, incident: Incident) -> str:
        # Application incidents intentionally deduplicate across member
        # repositories and services.  Service is a routing hint; the
        # application is the durable incident scope.
        if incident.application:
            return json.dumps(
                [
                    "application",
                    incident.source,
                    incident.external_id,
                    incident.environment,
                    incident.application.casefold(),
                ]
            )
        return cls.legacy_scope(incident)

    @staticmethod
    def _ensure_workspace_schema(db: sqlite3.Connection) -> None:
        columns = {row[1] for row in db.execute("PRAGMA table_info(catalog_workspaces)")}
        if "repository" in columns:
            return
        db.execute("ALTER TABLE catalog_workspaces RENAME TO catalog_workspaces_legacy")
        db.execute(
            """CREATE TABLE catalog_workspaces (
                task_id TEXT NOT NULL REFERENCES catalog_tasks(task_id),
                repository TEXT NOT NULL DEFAULT '', root TEXT NOT NULL,
                device INTEGER NOT NULL, inode INTEGER NOT NULL, active INTEGER NOT NULL,
                PRIMARY KEY(task_id, repository)
            )"""
        )
        db.execute(
            "INSERT INTO catalog_workspaces(task_id, repository, root, device, inode, active) "
            "SELECT task_id, '', root, device, inode, active FROM catalog_workspaces_legacy"
        )
        db.execute("DROP TABLE catalog_workspaces_legacy")

    def _has_marker(self, name: str) -> bool:
        with connect(self.path) as db:
            return (
                db.execute("SELECT 1 FROM runtime_migrations WHERE name = ?", (name,)).fetchone()
                is not None
            )

    @staticmethod
    def _bad(path: Path, error: BaseException | None = None) -> ValueError:
        return ValueError(f"cannot migrate task catalog: {path}")

    def _migrate_json(self) -> None:
        if self._has_marker(_MIGRATION):
            return
        payload: dict[str, Any] | None = None
        if self.json_path.exists():
            try:
                payload = json.loads(self.json_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise self._bad(self.json_path, error) from error
            if (
                not isinstance(payload, dict)
                or payload.get("version") != 1
                or not all(
                    key in payload and isinstance(payload[key], dict)
                    for key in ("tasks", "events", "workspaces", "leases")
                )
            ):
                raise self._bad(self.json_path)
        if payload is None:
            return
        with transaction(self.path) as db:
            if db.execute(
                "SELECT 1 FROM runtime_migrations WHERE name = ?", (_MIGRATION,)
            ).fetchone():
                return
            if payload is not None:
                for task_id, item in payload["tasks"].items():
                    try:
                        if (
                            not isinstance(item, dict)
                            or task_id != item["record"]["task_id"]
                            or item["state"] != item["record"]["state"]
                        ):
                            raise ValueError("invalid task")
                        task = TaskRecord.model_validate(item["record"])
                        incident = Incident.model_validate(item["incident"])
                        scope = str(item["scope"])
                        if scope not in {self.scope(incident), self.legacy_scope(incident)}:
                            raise ValueError("invalid scope")
                    except (KeyError, TypeError, ValueError) as error:
                        raise self._bad(self.json_path, error) from error
                    db.execute(
                        "INSERT INTO catalog_tasks VALUES (?, ?, ?, ?, ?)",
                        (
                            task_id,
                            scope,
                            task.state.value,
                            task.model_dump_json(),
                            incident.model_dump_json(),
                        ),
                    )
                for task_id, items in payload["events"].items():
                    if db.execute(
                        "SELECT 1 FROM catalog_tasks WHERE task_id = ?", (task_id,)
                    ).fetchone() is None or not isinstance(items, list):
                        raise self._bad(self.json_path)
                    for item in items:
                        try:
                            event = TaskEvent.model_validate(item)
                        except ValueError as error:
                            raise self._bad(self.json_path, error) from error
                        db.execute(
                            "INSERT INTO catalog_events(task_id, event) VALUES (?, ?)",
                            (task_id, event.model_dump_json()),
                        )
                for task_id, item in payload["workspaces"].items():
                    if (
                        db.execute(
                            "SELECT 1 FROM catalog_tasks WHERE task_id = ?", (task_id,)
                        ).fetchone()
                        is None
                    ):
                        raise self._bad(self.json_path)
                    try:
                        values = (
                            task_id,
                            str(item["root"]),
                            int(item["device"]),
                            int(item["inode"]),
                            int(item["active"]),
                        )
                    except (KeyError, TypeError, ValueError) as error:
                        raise self._bad(self.json_path, error) from error
                    db.execute(
                        "INSERT INTO catalog_workspaces VALUES (?, '', ?, ?, ?, ?)", values
                    )
                for task_id, item in payload["leases"].items():
                    if (
                        db.execute(
                            "SELECT 1 FROM catalog_tasks WHERE task_id = ?", (task_id,)
                        ).fetchone()
                        is None
                    ):
                        raise self._bad(self.json_path)
                    try:
                        values = (task_id, str(item["owner"]), float(item["expires"]))
                    except (KeyError, TypeError, ValueError) as error:
                        raise self._bad(self.json_path, error) from error
                    db.execute("INSERT INTO catalog_leases VALUES (?, ?, ?)", values)
            db.execute("INSERT INTO runtime_migrations(name) VALUES (?)", (_MIGRATION,))

    def _migrate_legacy(self) -> None:
        if self._has_marker(_MIGRATION):
            return
        # A present JSON catalog is the newer source of truth, including an
        # intentionally empty catalog. Never resurrect removed JSON data from
        # the older SQLite file.
        if self.json_path.exists():
            with transaction(self.path) as db:
                db.execute("INSERT INTO runtime_migrations(name) VALUES (?)", (_MIGRATION,))
            return
        rows: dict[str, list[sqlite3.Row]] = {}
        if self.legacy_path.exists() and self.legacy_path.resolve() != self.path.resolve():
            try:
                with closing(
                    sqlite3.connect(f"{self.legacy_path.resolve().as_uri()}?mode=ro", uri=True)
                ) as legacy:
                    legacy.row_factory = sqlite3.Row
                    rows = {
                        "tasks": legacy.execute(
                            "SELECT task_id, scope, state, record, incident FROM catalog_tasks"
                        ).fetchall(),
                        "events": legacy.execute(
                            "SELECT task_id, event FROM catalog_events ORDER BY sequence"
                        ).fetchall(),
                        "workspaces": legacy.execute(
                            "SELECT task_id, root, device, inode, active FROM catalog_workspaces"
                        ).fetchall(),
                        "leases": legacy.execute(
                            "SELECT task_id, owner, expires FROM catalog_leases"
                        ).fetchall(),
                    }
            except (OSError, sqlite3.Error) as error:
                raise ValueError(
                    f"cannot migrate legacy task catalog: {self.legacy_path}"
                ) from error
        with transaction(self.path) as db:
            if db.execute(
                "SELECT 1 FROM runtime_migrations WHERE name = ?", (_MIGRATION,)
            ).fetchone():
                return
            for row in rows.get("tasks", []):
                try:
                    task = TaskRecord.model_validate_json(row["record"])
                    incident = Incident.model_validate_json(row["incident"])
                    if (
                        row["task_id"] != task.task_id
                        or row["state"] != task.state.value
                        or row["scope"] not in {self.scope(incident), self.legacy_scope(incident)}
                    ):
                        raise ValueError("legacy task identity mismatch")
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        f"cannot migrate legacy task catalog: {self.legacy_path}"
                    ) from error
                db.execute(
                    "INSERT INTO catalog_tasks VALUES (?, ?, ?, ?, ?)",
                    (
                        row["task_id"],
                        row["scope"],
                        task.state.value,
                        task.model_dump_json(),
                        incident.model_dump_json(),
                    ),
                )
            for row in rows.get("events", []):
                if (
                    db.execute(
                        "SELECT 1 FROM catalog_tasks WHERE task_id = ?", (row["task_id"],)
                    ).fetchone()
                    is None
                ):
                    raise ValueError(f"cannot migrate legacy task catalog: {self.legacy_path}")
                event = TaskEvent.model_validate_json(row["event"])
                db.execute(
                    "INSERT INTO catalog_events(task_id, event) VALUES (?, ?)",
                    (row["task_id"], event.model_dump_json()),
                )
            for row in rows.get("workspaces", []):
                if (
                    db.execute(
                        "SELECT 1 FROM catalog_tasks WHERE task_id = ?", (row["task_id"],)
                    ).fetchone()
                    is None
                ):
                    raise ValueError(f"cannot migrate legacy task catalog: {self.legacy_path}")
                db.execute(
                    "INSERT INTO catalog_workspaces VALUES (?, '', ?, ?, ?, ?)", tuple(row)
                )
            for row in rows.get("leases", []):
                if (
                    db.execute(
                        "SELECT 1 FROM catalog_tasks WHERE task_id = ?", (row["task_id"],)
                    ).fetchone()
                    is None
                ):
                    raise ValueError(f"cannot migrate legacy task catalog: {self.legacy_path}")
                db.execute("INSERT INTO catalog_leases VALUES (?, ?, ?)", tuple(row))
            db.execute("INSERT INTO runtime_migrations(name) VALUES (?)", (_MIGRATION,))

    def create(
        self, task: TaskRecord, incident: Incident, events: list[TaskEvent] | None = None
    ) -> tuple[TaskRecord, bool]:
        with transaction(self.path) as db:
            row = db.execute(
                "SELECT record FROM catalog_tasks WHERE scope IN (?, ?)",
                (self.scope(incident), self.legacy_scope(incident)),
            ).fetchone()
            if row:
                return TaskRecord.model_validate_json(row["record"]), False
            db.execute(
                "INSERT INTO catalog_tasks VALUES (?, ?, ?, ?, ?)",
                (
                    task.task_id,
                    self.scope(incident),
                    task.state.value,
                    task.model_dump_json(),
                    incident.model_dump_json(),
                ),
            )
            for event in events or [TaskEvent(type="task.received")]:
                db.execute(
                    "INSERT INTO catalog_events(task_id, event) VALUES (?, ?)",
                    (task.task_id, event.model_dump_json()),
                )
        return task, True

    def load(self, task_id: str) -> TaskRecord:
        with connect(self.path) as db:
            row = db.execute(
                "SELECT record FROM catalog_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise FileNotFoundError(task_id)
        return TaskRecord.model_validate_json(row["record"])

    def incident(self, task_id: str) -> Incident:
        with connect(self.path) as db:
            row = db.execute(
                "SELECT incident FROM catalog_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise FileNotFoundError(task_id)
        return Incident.model_validate_json(row["incident"])

    def save(self, task: TaskRecord, event: TaskEvent | None = None) -> None:
        with transaction(self.path) as db:
            if (
                db.execute(
                    "SELECT 1 FROM catalog_tasks WHERE task_id = ?", (task.task_id,)
                ).fetchone()
                is None
            ):
                raise FileNotFoundError(task.task_id)
            db.execute(
                "UPDATE catalog_tasks SET state = ?, record = ? WHERE task_id = ?",
                (task.state.value, task.model_dump_json(), task.task_id),
            )
            if event:
                db.execute(
                    "INSERT INTO catalog_events(task_id, event) VALUES (?, ?)",
                    (task.task_id, event.model_dump_json()),
                )

    def transition(
        self, task_id: str, state, *, event: TaskEvent | None = None, **updates: object
    ) -> TaskRecord:
        with transaction(self.path) as db:
            row = db.execute(
                "SELECT record FROM catalog_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise FileNotFoundError(task_id)
            task = TaskRecord.model_validate_json(row["record"])
            triage = updates.get("triage")
            triage_release = bool(
                event
                and event.type == "triage.released"
                and task.triage
                and task.triage.get("route") == "operator_review"
                and isinstance(triage, dict)
                and triage.get("route") == "agent"
                and triage.get("operator_released") is True
            )
            validate_transition(task.state, state, triage_release=triage_release)
            for key, value in updates.items():
                if key not in TaskRecord.model_fields:
                    raise ValueError(f"unknown task field: {key}")
                setattr(task, key, value)
            task.state = state
            task.updated_at = utc_now()
            db.execute(
                "UPDATE catalog_tasks SET state = ?, record = ? WHERE task_id = ?",
                (task.state.value, task.model_dump_json(), task_id),
            )
            if event:
                db.execute(
                    "INSERT INTO catalog_events(task_id, event) VALUES (?, ?)",
                    (task_id, event.model_dump_json()),
                )
            return task

    def append_event(self, task_id: str, event: TaskEvent) -> None:
        self.load(task_id)
        with transaction(self.path) as db:
            db.execute(
                "INSERT INTO catalog_events(task_id, event) VALUES (?, ?)",
                (task_id, event.model_dump_json()),
            )

    def events(self, task_id: str) -> list[TaskEvent]:
        self.load(task_id)
        with connect(self.path) as db:
            rows = db.execute(
                "SELECT event FROM catalog_events WHERE task_id = ? ORDER BY sequence", (task_id,)
            ).fetchall()
        return [TaskEvent.model_validate_json(row["event"]) for row in rows]

    def tasks(self) -> list[TaskRecord]:
        with connect(self.path) as db:
            rows = db.execute("SELECT record FROM catalog_tasks").fetchall()
        return sorted(
            (TaskRecord.model_validate_json(row["record"]) for row in rows),
            key=lambda task: task.created_at,
        )

    def register_workspace(self, task_id: str, root: Path, repository: str = "") -> None:
        self.load(task_id)
        root = root.resolve()
        stat = root.stat()
        with transaction(self.path) as db:
            db.execute(
                "INSERT OR REPLACE INTO catalog_workspaces "
                "(task_id, repository, root, device, inode, active) VALUES (?, ?, ?, ?, ?, 1)",
                (task_id, repository, str(root), stat.st_dev, stat.st_ino),
            )

    def verify_workspace(self, task_id: str, root: Path, repository: str = "") -> None:
        with connect(self.path) as db:
            row = db.execute(
                "SELECT root, device, inode, active FROM catalog_workspaces "
                "WHERE task_id = ? AND repository = ?",
                (task_id, repository),
            ).fetchone()
        if row is None:
            return self.register_workspace(task_id, root, repository)
        stat = root.stat()
        if not row["active"] or (str(root.resolve()), stat.st_dev, stat.st_ino) != (
            row["root"],
            row["device"],
            row["inode"],
        ):
            raise ValueError("workspace identity differs from its durable registration")

    def release_workspace(self, task_id: str, repository: str | None = None) -> None:
        with transaction(self.path) as db:
            if repository is None:
                db.execute("UPDATE catalog_workspaces SET active = 0 WHERE task_id = ?", (task_id,))
            else:
                db.execute(
                    "UPDATE catalog_workspaces SET active = 0 WHERE task_id = ? AND repository = ?",
                    (task_id, repository),
                )

    def acquire(self, task_id: str, owner: str, ttl: float) -> bool:
        self.load(task_id)
        with transaction(self.path) as db:
            now = time.time()
            row = db.execute(
                "SELECT owner, expires FROM catalog_leases WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row and row["owner"] != owner and row["expires"] > now:
                return False
            db.execute(
                "INSERT OR REPLACE INTO catalog_leases VALUES (?, ?, ?)",
                (task_id, owner, now + ttl),
            )
        return True

    def release(self, task_id: str, owner: str) -> None:
        with transaction(self.path) as db:
            db.execute(
                "DELETE FROM catalog_leases WHERE task_id = ? AND owner = ?", (task_id, owner)
            )
