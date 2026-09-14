"""Transactional task state, event journal, and workspace identities.

Filesystem task folders are human-readable projections and artifact storage. Queue
selection and recovery use this database, independently of directory placement.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .models import Incident, TaskEvent, TaskRecord


class TaskCatalog:
    def __init__(self, path: Path) -> None:
        self.path = path
        with closing(self.connect()) as connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS catalog_tasks ("
                "task_id TEXT PRIMARY KEY, scope TEXT UNIQUE NOT NULL, state TEXT NOT NULL, "
                "record TEXT NOT NULL, incident TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS catalog_events ("
                "sequence INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, "
                "event TEXT NOT NULL);"
                "CREATE INDEX IF NOT EXISTS catalog_events_task ON catalog_events(task_id);"
                "CREATE TABLE IF NOT EXISTS catalog_workspaces ("
                "task_id TEXT PRIMARY KEY, root TEXT NOT NULL, device INTEGER NOT NULL, "
                "inode INTEGER NOT NULL, active INTEGER NOT NULL);"
                "CREATE TABLE IF NOT EXISTS catalog_leases ("
                "task_id TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL);"
                "PRAGMA user_version=1;"
            )

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    @staticmethod
    def scope(incident: Incident) -> str:
        return json.dumps(
            [
                incident.source,
                incident.external_id,
                incident.repository.casefold(),
                incident.environment,
            ]
        )

    def create(
        self, task: TaskRecord, incident: Incident, events: list[TaskEvent] | None = None
    ) -> tuple[TaskRecord, bool]:
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT record FROM catalog_tasks WHERE scope=?", (self.scope(incident),)
            ).fetchone()
            if existing:
                return TaskRecord.model_validate_json(existing[0]), False
            connection.execute(
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
                connection.execute(
                    "INSERT INTO catalog_events(task_id,event) VALUES (?,?)",
                    (task.task_id, event.model_dump_json()),
                )
        return task, True

    def load(self, task_id: str) -> TaskRecord:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT record FROM catalog_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        if row is None:
            raise FileNotFoundError(task_id)
        return TaskRecord.model_validate_json(row[0])

    def incident(self, task_id: str) -> Incident:
        self.load(task_id)
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT incident FROM catalog_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return Incident.model_validate_json(row[0])

    def save(self, task: TaskRecord, event: TaskEvent | None = None) -> None:
        with closing(self.connect()) as connection, connection:
            updated = connection.execute(
                "UPDATE catalog_tasks SET state=?, record=? WHERE task_id=?",
                (task.state.value, task.model_dump_json(), task.task_id),
            )
            if not updated.rowcount:
                raise FileNotFoundError(task.task_id)
            if event:
                connection.execute(
                    "INSERT INTO catalog_events(task_id,event) VALUES (?,?)",
                    (task.task_id, event.model_dump_json()),
                )

    def append_event(self, task_id: str, event: TaskEvent) -> None:
        self.load(task_id)
        with closing(self.connect()) as connection, connection:
            connection.execute(
                "INSERT INTO catalog_events(task_id,event) VALUES (?,?)",
                (task_id, event.model_dump_json()),
            )

    def events(self, task_id: str) -> list[TaskEvent]:
        self.load(task_id)
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT event FROM catalog_events WHERE task_id=? ORDER BY sequence", (task_id,)
            ).fetchall()
        return [TaskEvent.model_validate_json(row[0]) for row in rows]

    def tasks(self) -> list[TaskRecord]:
        with closing(self.connect()) as connection:
            rows = connection.execute("SELECT record FROM catalog_tasks").fetchall()
        return sorted(
            (TaskRecord.model_validate_json(row[0]) for row in rows),
            key=lambda task: task.created_at,
        )

    def register_workspace(self, task_id: str, root: Path) -> None:
        self.load(task_id)
        root = root.resolve()
        stat = root.stat()
        with closing(self.connect()) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO catalog_workspaces VALUES (?,?,?,?,1)",
                (task_id, str(root), stat.st_dev, stat.st_ino),
            )

    def verify_workspace(self, task_id: str, root: Path) -> None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT root, device, inode, active FROM catalog_workspaces WHERE task_id=?",
                (task_id,),
            ).fetchone()
        if row is None:
            self.register_workspace(task_id, root)
            return
        stat = root.stat()
        if not row[3] or (str(root.resolve()), stat.st_dev, stat.st_ino) != row[:3]:
            raise ValueError("workspace identity differs from its durable registration")

    def release_workspace(self, task_id: str) -> None:
        with closing(self.connect()) as connection, connection:
            connection.execute("UPDATE catalog_workspaces SET active=0 WHERE task_id=?", (task_id,))

    def acquire(self, task_id: str, owner: str, ttl: float) -> bool:
        self.load(task_id)
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner,expires FROM catalog_leases WHERE task_id=?", (task_id,)
            ).fetchone()
            if row and row[0] != owner and row[1] > time.time():
                return False
            connection.execute(
                "INSERT OR REPLACE INTO catalog_leases VALUES (?,?,?)",
                (task_id, owner, time.time() + ttl),
            )
        return True

    def release(self, task_id: str, owner: str) -> None:
        with closing(self.connect()) as connection, connection:
            connection.execute(
                "DELETE FROM catalog_leases WHERE task_id=? AND owner=?", (task_id, owner)
            )
