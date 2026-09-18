"""Durable file-backed task catalog."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .file_store import JsonFile
from .lifecycle_graph import validate_transition
from .models import Incident, TaskEvent, TaskRecord


def _empty() -> dict:
    return {"version": 1, "tasks": {}, "events": {}, "workspaces": {}, "leases": {}}


class TaskCatalog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if self.path.suffix == ".sqlite3":
            self.path = self.path.with_suffix(".json")
        self.store = JsonFile(self.path, _empty)
        self._migrate_sqlite()

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

    def _migrate_sqlite(self) -> None:
        legacy = self.path.with_name("tasks.sqlite3")
        if not legacy.exists() or self.path.exists():
            return
        try:
            with closing(sqlite3.connect(f"{legacy.resolve().as_uri()}?mode=ro", uri=True)) as db:
                tasks = db.execute(
                    "SELECT task_id, scope, state, record, incident FROM catalog_tasks"
                ).fetchall()
                events = db.execute(
                    "SELECT task_id, event FROM catalog_events ORDER BY sequence"
                ).fetchall()
                workspaces = db.execute(
                    "SELECT task_id, root, device, inode, active FROM catalog_workspaces"
                ).fetchall()
                leases = db.execute("SELECT task_id, owner, expires FROM catalog_leases").fetchall()
            with self.store.transaction() as data:
                # The transaction rechecks the destination after taking the
                # lock, so a concurrent writer always wins the migration race.
                if self.path.exists():
                    return
                for task_id, scope, state, record, incident in tasks:
                    data["tasks"][task_id] = {
                        "scope": scope,
                        "state": state,
                        "record": json.loads(record),
                        "incident": json.loads(incident),
                    }
                for task_id, event in events:
                    data["events"].setdefault(task_id, []).append(json.loads(event))
                for task_id, root, device, inode, active in workspaces:
                    data["workspaces"][task_id] = {
                        "root": root,
                        "device": device,
                        "inode": inode,
                        "active": active,
                    }
                for task_id, owner, expires in leases:
                    data["leases"][task_id] = {"owner": owner, "expires": expires}
        except (OSError, sqlite3.Error) as error:
            raise ValueError(f"cannot migrate legacy task catalog: {legacy}") from error

    def create(
        self, task: TaskRecord, incident: Incident, events: list[TaskEvent] | None = None
    ) -> tuple[TaskRecord, bool]:
        with self.store.transaction() as data:
            for item in data["tasks"].values():
                if item["scope"] == self.scope(incident):
                    return TaskRecord.model_validate(item["record"]), False
            data["tasks"][task.task_id] = {
                "scope": self.scope(incident),
                "state": task.state.value,
                "record": task.model_dump(mode="json"),
                "incident": incident.model_dump(mode="json"),
            }
            data["events"][task.task_id] = [
                e.model_dump(mode="json") for e in (events or [TaskEvent(type="task.received")])
            ]
        return task, True

    def load(self, task_id: str) -> TaskRecord:
        item = self.store.read()["tasks"].get(task_id)
        if item is None:
            raise FileNotFoundError(task_id)
        return TaskRecord.model_validate(item["record"])

    def incident(self, task_id: str) -> Incident:
        item = self.store.read()["tasks"].get(task_id)
        if item is None:
            raise FileNotFoundError(task_id)
        return Incident.model_validate(item["incident"])

    def save(self, task: TaskRecord, event: TaskEvent | None = None) -> None:
        with self.store.transaction() as data:
            item = data["tasks"].get(task.task_id)
            if item is None:
                raise FileNotFoundError(task.task_id)
            item.update(state=task.state.value, record=task.model_dump(mode="json"))
            if event:
                data["events"].setdefault(task.task_id, []).append(event.model_dump(mode="json"))

    def transition(
        self, task_id: str, state, *, event: TaskEvent | None = None, **updates: object
    ) -> TaskRecord:
        with self.store.transaction() as data:
            item = data["tasks"].get(task_id)
            if item is None:
                raise FileNotFoundError(task_id)
            task = TaskRecord.model_validate(item["record"])
            validate_transition(task.state, state)
            for key, value in updates.items():
                if key not in TaskRecord.model_fields:
                    raise ValueError(f"unknown task field: {key}")
                setattr(task, key, value)
            task.state = state
            item.update(state=task.state.value, record=task.model_dump(mode="json"))
            if event:
                data["events"].setdefault(task_id, []).append(event.model_dump(mode="json"))
            return task

    def append_event(self, task_id: str, event: TaskEvent) -> None:
        self.load(task_id)
        with self.store.transaction() as data:
            data["events"].setdefault(task_id, []).append(event.model_dump(mode="json"))

    def events(self, task_id: str) -> list[TaskEvent]:
        self.load(task_id)
        return [
            TaskEvent.model_validate(item) for item in self.store.read()["events"].get(task_id, [])
        ]

    def tasks(self) -> list[TaskRecord]:
        return sorted(
            (
                TaskRecord.model_validate(item["record"])
                for item in self.store.read()["tasks"].values()
            ),
            key=lambda t: t.created_at,
        )

    def register_workspace(self, task_id: str, root: Path) -> None:
        self.load(task_id)
        root = root.resolve()
        stat = root.stat()
        with self.store.transaction() as data:
            data["workspaces"][task_id] = {
                "root": str(root),
                "device": stat.st_dev,
                "inode": stat.st_ino,
                "active": 1,
            }

    def verify_workspace(self, task_id: str, root: Path) -> None:
        item = self.store.read()["workspaces"].get(task_id)
        if item is None:
            return self.register_workspace(task_id, root)
        stat = root.stat()
        if not item["active"] or (str(root.resolve()), stat.st_dev, stat.st_ino) != (
            item["root"],
            item["device"],
            item["inode"],
        ):
            raise ValueError("workspace identity differs from its durable registration")

    def release_workspace(self, task_id: str) -> None:
        with self.store.transaction() as data:
            if task_id in data["workspaces"]:
                data["workspaces"][task_id]["active"] = 0

    def acquire(self, task_id: str, owner: str, ttl: float) -> bool:
        self.load(task_id)
        with self.store.transaction() as data:
            now = time.time()
            row = data["leases"].get(task_id)
            if row and row["owner"] != owner and row["expires"] > now:
                return False
            data["leases"][task_id] = {"owner": owner, "expires": now + ttl}
        return True

    def release(self, task_id: str, owner: str) -> None:
        with self.store.transaction() as data:
            row = data["leases"].get(task_id)
            if row and row["owner"] == owner:
                del data["leases"][task_id]
