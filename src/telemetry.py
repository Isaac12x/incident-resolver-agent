"""Persistent, bounded-cardinality runtime metrics and credential-free audit logs."""

from __future__ import annotations

import fcntl
import json
import re
import sqlite3
from pathlib import Path

from .file_store import JsonFile
from .models import utc_now


class Telemetry:
    def __init__(self, root: Path) -> None:
        self.database = root / "telemetry.json"
        self._metrics = JsonFile(
            self.database, lambda: {"schema_version": 1, "migrated": False, "metrics": {}}
        )
        self.log = root / "logs" / "runtime.jsonl"
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy(root / "telemetry.sqlite3")

    def _migrate_legacy(self, database: Path) -> None:
        if self.database.exists() or not database.is_file():
            return
        try:
            connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                rows = connection.execute(
                    "SELECT name,calls,failures,seconds FROM metrics ORDER BY name"
                ).fetchall()
            finally:
                connection.close()
            with self._metrics.transaction() as document:
                if document.get("migrated"):
                    return
                document["schema_version"] = 1
                document["migrated"] = True
                document["metrics"] = {
                    str(name): {
                        "calls": int(calls),
                        "failures": int(failures),
                        "seconds": float(seconds),
                    }
                    for name, calls, failures, seconds in rows
                }
        except sqlite3.OperationalError as error:
            if "no such table" not in str(error).lower():
                raise ValueError(f"cannot read legacy telemetry database: {database}") from error
        except sqlite3.DatabaseError as error:
            raise ValueError(f"cannot read legacy telemetry database: {database}") from error
        except OSError as error:
            raise ValueError(f"cannot read legacy telemetry database: {database}") from error

    def record(
        self, name: str, *, success: bool = True, seconds: float = 0, task_id: str | None = None
    ) -> None:
        # Avoid incident IDs, URL paths and user-supplied strings becoming metric labels.
        allowed = {"http.request", "tool.shell", "agent.run", "task.transition", "task.created"}
        if name not in allowed:
            raise ValueError("unsupported telemetry metric")
        if seconds < 0:
            raise ValueError("metric duration cannot be negative")
        if task_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task_id):
            raise ValueError("invalid telemetry task id")
        with self._metrics.transaction() as document:
            document["migrated"] = True
            metrics = document.setdefault("metrics", {})
            metric = metrics.setdefault(name, {"calls": 0, "failures": 0, "seconds": 0.0})
            metric["calls"] += 1
            metric["failures"] += int(not success)
            metric["seconds"] += seconds
        entry = {
            "time": utc_now().isoformat(),
            "name": name,
            "success": success,
            "seconds": round(seconds, 6),
            "task_id": task_id,
        }
        with self.log.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if self.log.exists() and self.log.stat().st_size > 5_000_000:
                    self.log.replace(self.log.with_suffix(".jsonl.1"))
                with self.log.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry) + "\n")
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def snapshot(self) -> list[dict[str, str | int | float]]:
        document = self._metrics.read()
        metrics = document.get("metrics")
        if document.get("schema_version") != 1 or not isinstance(metrics, dict):
            raise ValueError(f"invalid telemetry document: {self.database}")
        return [
            {
                "name": name,
                "calls": value["calls"],
                "failures": value["failures"],
                "seconds": value["seconds"],
            }
            for name, value in sorted(metrics.items())
        ]

    def prometheus(self) -> str:
        lines = [
            "# TYPE incident_harness_calls_total counter",
            "# TYPE incident_harness_failures_total counter",
            "# TYPE incident_harness_duration_seconds_total counter",
        ]
        for row in self.snapshot():
            label = str(row["name"])
            for field, metric in (
                ("calls", "calls_total"),
                ("failures", "failures_total"),
                ("seconds", "duration_seconds_total"),
            ):
                lines.append(f'incident_harness_{metric}{{operation="{label}"}} {row[field]}')
        return "\n".join(lines) + "\n"
