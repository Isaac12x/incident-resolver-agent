"""Persistent runtime metrics in the shared SQLite runtime store."""

from __future__ import annotations

import fcntl
import json
import re
import sqlite3
from pathlib import Path

from .models import utc_now
from .sqlite_store import connect, transaction

_METRIC_NAMES = {"http.request", "tool.shell", "agent.run", "task.transition", "task.created"}


class Telemetry:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.database = self.root / "runtime.sqlite3"
        self.log = self.root / "logs" / "runtime.jsonl"
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()
        self._migrate_legacy()

    def _ensure_schema(self) -> None:
        with transaction(self.database) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS telemetry_metrics ("
                "name TEXT PRIMARY KEY, calls INTEGER NOT NULL, failures INTEGER NOT NULL, "
                "seconds REAL NOT NULL)"
            )

    def _read_json_metrics(self, path: Path) -> dict[str, dict[str, int | float]]:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError(f"invalid telemetry document: {path}") from error
        metrics = document.get("metrics") if isinstance(document, dict) else None
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != 1
            or not isinstance(metrics, dict)
        ):
            raise ValueError(f"invalid telemetry document: {path}")
        result = {}
        for name, value in metrics.items():
            if not isinstance(name, str) or not isinstance(value, dict):
                raise ValueError(f"invalid telemetry document: {path}")
            try:
                calls, failures, seconds = (
                    int(value["calls"]),
                    int(value["failures"]),
                    float(value["seconds"]),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"invalid telemetry document: {path}") from error
            if calls < 0 or failures < 0 or seconds < 0 or failures > calls:
                raise ValueError(f"invalid telemetry document: {path}")
            result[name] = {"calls": calls, "failures": failures, "seconds": seconds}
        return result

    def _migrate_legacy(self) -> None:
        json_path, legacy = self.root / "telemetry.json", self.root / "telemetry.sqlite3"
        marker = "telemetry:v1"
        with transaction(self.database) as db:
            if db.execute("SELECT 1 FROM runtime_migrations WHERE name=?", (marker,)).fetchone():
                return
            if json_path.exists():
                metrics = self._read_json_metrics(json_path)
                for name, value in metrics.items():
                    db.execute(
                        "INSERT INTO telemetry_metrics VALUES(?,?,?,?) "
                        "ON CONFLICT(name) DO UPDATE SET calls=excluded.calls, "
                        "failures=excluded.failures, seconds=excluded.seconds",
                        (name, value["calls"], value["failures"], value["seconds"]),
                    )
            elif legacy.is_file():
                try:
                    old = sqlite3.connect(legacy.resolve().as_uri() + "?mode=ro", uri=True)
                    try:
                        rows = old.execute(
                            "SELECT name,calls,failures,seconds FROM metrics ORDER BY name"
                        ).fetchall()
                    finally:
                        old.close()
                except sqlite3.OperationalError as error:
                    if "no such table" in str(error).lower():
                        rows = []
                    else:
                        raise ValueError(
                            f"cannot read legacy telemetry database: {legacy}"
                        ) from error
                except (sqlite3.DatabaseError, OSError) as error:
                    raise ValueError(f"cannot read legacy telemetry database: {legacy}") from error
                for name, calls, failures, seconds in rows:
                    db.execute(
                        "INSERT INTO telemetry_metrics VALUES(?,?,?,?) "
                        "ON CONFLICT(name) DO UPDATE SET calls=excluded.calls, "
                        "failures=excluded.failures, seconds=excluded.seconds",
                        (str(name), int(calls), int(failures), float(seconds)),
                    )
            db.execute("INSERT OR IGNORE INTO runtime_migrations(name) VALUES(?)", (marker,))

    def record(
        self, name: str, *, success: bool = True, seconds: float = 0, task_id: str | None = None
    ) -> None:
        if name not in _METRIC_NAMES:
            raise ValueError("unsupported telemetry metric")
        if seconds < 0:
            raise ValueError("metric duration cannot be negative")
        if task_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task_id):
            raise ValueError("invalid telemetry task id")
        with transaction(self.database) as db:
            db.execute(
                "INSERT INTO telemetry_metrics VALUES(?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET calls=calls+1, "
                "failures=failures+excluded.failures, seconds=seconds+excluded.seconds",
                (name, 1, int(not success), seconds),
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
        with connect(self.database) as db:
            rows = db.execute(
                "SELECT name,calls,failures,seconds FROM telemetry_metrics ORDER BY name"
            ).fetchall()
        return [
            {
                "name": row["name"],
                "calls": row["calls"],
                "failures": row["failures"],
                "seconds": row["seconds"],
            }
            for row in rows
        ]

    def prometheus(self) -> str:
        lines = [
            "# TYPE incident_harness_calls_total counter",
            "# TYPE incident_harness_failures_total counter",
            "# TYPE incident_harness_duration_seconds_total counter",
        ]
        for row in self.snapshot():
            for field, metric in (
                ("calls", "calls_total"),
                ("failures", "failures_total"),
                ("seconds", "duration_seconds_total"),
            ):
                lines.append(f'incident_harness_{metric}{{operation="{row["name"]}"}} {row[field]}')
        return "\n".join(lines) + "\n"
