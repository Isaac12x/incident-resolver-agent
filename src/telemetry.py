"""Persistent, bounded-cardinality runtime metrics and credential-free audit logs."""

from __future__ import annotations

import fcntl
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .models import utc_now


class Telemetry:
    def __init__(self, root: Path) -> None:
        self.database = root / "telemetry.sqlite3"
        self.log = root / "logs" / "runtime.jsonl"
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metrics (name TEXT PRIMARY KEY, "
                "calls INTEGER NOT NULL, failures INTEGER NOT NULL, seconds REAL NOT NULL)"
            )

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
        with closing(sqlite3.connect(self.database, timeout=30)) as connection, connection:
            connection.execute(
                "INSERT INTO metrics VALUES (?,1,?,?) ON CONFLICT(name) DO UPDATE SET "
                "calls=calls+1, failures=failures+excluded.failures, "
                "seconds=seconds+excluded.seconds",
                (name, int(not success), seconds),
            )
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
        with closing(sqlite3.connect(self.database)) as connection:
            rows = connection.execute(
                "SELECT name,calls,failures,seconds FROM metrics ORDER BY name"
            ).fetchall()
        return [
            dict(zip(("name", "calls", "failures", "seconds"), row, strict=True)) for row in rows
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
