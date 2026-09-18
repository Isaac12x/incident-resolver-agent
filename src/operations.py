"""SQLite-backed operation attempts and recovery checkpoints."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .sqlite_store import connect, transaction


class OperationBudgetExceeded(RuntimeError):
    """The operation or task-wide retry budget is exhausted."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class OperationLedger:
    def __init__(
        self,
        path: Path | str,
        *,
        max_attempts: int = 3,
        overall_cap: int = 10,
        database: Path | str | None = None,
        namespace: str | None = None,
    ) -> None:
        if max_attempts < 1 or overall_cap < 1:
            raise ValueError("operation budgets must be positive")
        supplied = Path(path)
        self.legacy_source: Path | None = None
        if database is None:
            if supplied.suffix == ".sqlite3":
                database, namespace = supplied, namespace or supplied.stem
                if namespace != supplied.stem:
                    self.legacy_source = supplied.parent / "operations" / f"{namespace}.json"
            elif supplied.suffix:
                self.legacy_source = supplied
                database, namespace = (
                    supplied.parent / "runtime.sqlite3",
                    namespace or supplied.stem,
                )
            else:
                database, namespace = supplied / "runtime.sqlite3", namespace or supplied.name
        elif namespace:
            self.legacy_source = Path(database).parent / "operations" / f"{namespace}.json"
        self.database, self.namespace = Path(database), namespace or "default"
        self.max_attempts, self.overall_cap = max_attempts, overall_cap
        self._ensure_schema()
        self._migrate_legacy()

    def _ensure_schema(self) -> None:
        with transaction(self.database) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS operation_current (
                namespace TEXT NOT NULL, operation TEXT NOT NULL, revision TEXT NOT NULL,
                attempts INTEGER NOT NULL, status TEXT NOT NULL, intent_id TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT, metadata TEXT NOT NULL,
                outcome TEXT, PRIMARY KEY(namespace, operation, revision))""")
            db.execute("""CREATE TABLE IF NOT EXISTS operation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL,
                operation TEXT NOT NULL, revision TEXT NOT NULL, attempts INTEGER NOT NULL,
                status TEXT NOT NULL, intent_id TEXT NOT NULL, started_at TEXT NOT NULL,
                finished_at TEXT, metadata TEXT NOT NULL, outcome TEXT, event TEXT NOT NULL,
                created_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS operation_budgets (
                namespace TEXT PRIMARY KEY, attempts INTEGER NOT NULL)""")

    def _migrate_legacy(self) -> None:
        marker = f"operations:{self.namespace}:json:v1"
        with connect(self.database) as db:
            if db.execute("SELECT 1 FROM runtime_migrations WHERE name=?", (marker,)).fetchone():
                return
        if self.legacy_source is None or not self.legacy_source.exists():
            with transaction(self.database) as db:
                db.execute("INSERT OR IGNORE INTO runtime_migrations(name) VALUES(?)", (marker,))
            return
        try:
            document = json.loads(self.legacy_source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError(f"invalid operation ledger: {self.legacy_source}") from error
        operations = document.get("operations") if isinstance(document, dict) else None
        history = document.get("history", []) if isinstance(document, dict) else None
        attempts = document.get("attempts") if isinstance(document, dict) else None
        if (
            not isinstance(document, dict)
            or document.get("version") != 1
            or not isinstance(operations, dict)
            or not isinstance(history, list)
            or not isinstance(attempts, int)
            or attempts < 0
        ):
            raise ValueError(f"invalid operation ledger: {self.legacy_source}")
        for record in operations.values():
            if (
                not isinstance(record, dict)
                or not all(
                    isinstance(record.get(k), str)
                    for k in ("operation", "revision", "status", "intent_id", "started_at")
                )
                or not isinstance(record.get("attempts"), int)
                or record["attempts"] < 1
            ):
                raise ValueError(f"invalid operation ledger: {self.legacy_source}")
            if record["status"] not in {"started", "succeeded", "failed"}:
                raise ValueError(f"invalid operation ledger: {self.legacy_source}")
            if not isinstance(record.get("metadata", {}), dict) or (
                "outcome" in record and not isinstance(record["outcome"], dict)
            ):
                raise ValueError(f"invalid operation ledger: {self.legacy_source}")
            if f"{record['operation']}\0{record['revision']}" not in operations:
                raise ValueError(f"invalid operation ledger: {self.legacy_source}")
        for event in history:
            if not isinstance(event, dict) or not all(
                k in event
                for k in (
                    "operation",
                    "revision",
                    "attempts",
                    "status",
                    "intent_id",
                    "started_at",
                    "event",
                )
            ):
                raise ValueError(f"invalid operation ledger: {self.legacy_source}")
            if not isinstance(event["attempts"], int) or event["attempts"] < 1:
                raise ValueError(f"invalid operation ledger: {self.legacy_source}")
        if attempts < sum(record["attempts"] for record in operations.values()):
            raise ValueError(f"invalid operation ledger: {self.legacy_source}")
        with transaction(self.database) as db:
            if db.execute("SELECT 1 FROM runtime_migrations WHERE name=?", (marker,)).fetchone():
                return
            db.execute(
                "INSERT INTO operation_budgets(namespace, attempts) VALUES(?,?)",
                (self.namespace, attempts),
            )
            for record in operations.values():
                db.execute(
                    """INSERT INTO operation_current
                    (namespace, operation, revision, attempts, status, intent_id, started_at,
                     finished_at, metadata, outcome) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        self.namespace,
                        record["operation"],
                        record["revision"],
                        record["attempts"],
                        record["status"],
                        record["intent_id"],
                        record["started_at"],
                        record.get("finished_at"),
                        json.dumps(record.get("metadata", {}), sort_keys=True),
                        json.dumps(record["outcome"], sort_keys=True)
                        if "outcome" in record
                        else None,
                    ),
                )
            for event in history:
                db.execute(
                    """INSERT INTO operation_history
                    (namespace, operation, revision, attempts, status, intent_id, started_at,
                    finished_at, metadata, outcome, event, created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        self.namespace,
                        event["operation"],
                        event["revision"],
                        event["attempts"],
                        event["status"],
                        event["intent_id"],
                        event["started_at"],
                        event.get("finished_at"),
                        json.dumps(event.get("metadata", {}), sort_keys=True),
                        json.dumps(event["outcome"], sort_keys=True)
                        if "outcome" in event
                        else None,
                        event["event"],
                        event.get("finished_at", event["started_at"]),
                    ),
                )
            db.execute("INSERT INTO runtime_migrations(name) VALUES(?)", (marker,))

    def _key(self, operation: str, revision: str) -> tuple[str, str]:
        if not operation.strip() or not revision.strip():
            raise ValueError("operation and revision are required")
        return operation, revision

    def _record(self, row: Any) -> dict[str, Any]:
        result = dict(row)
        for field in ("metadata", "outcome"):
            if result.get(field) is not None:
                result[field] = json.loads(result[field])
        for field in ("finished_at", "outcome"):
            if result.get(field) is None:
                result.pop(field, None)
        result.pop("namespace", None)
        return result

    def begin(
        self,
        operation: str,
        revision: str,
        *,
        metadata: dict[str, Any] | None = None,
        reconcile: bool = False,
        reuse_success: bool = True,
    ) -> dict[str, Any]:
        operation, revision = self._key(operation, revision)
        with transaction(self.database) as db:
            row = db.execute(
                "SELECT * FROM operation_current WHERE namespace=? AND operation=? AND revision=?",
                (self.namespace, operation, revision),
            ).fetchone()
            if row and row["status"] == "succeeded" and reuse_success:
                return self._record(row)
            if row and row["status"] == "started" and reconcile:
                return self._record(row)
            prior = int(row["attempts"]) if row else 0
            budget = db.execute(
                "SELECT attempts FROM operation_budgets WHERE namespace=?", (self.namespace,)
            ).fetchone()
            total = int(budget[0]) if budget else 0
            if total >= self.overall_cap or prior >= self.max_attempts:
                raise OperationBudgetExceeded(f"retry budget exhausted for {operation}")
            record = {
                "operation": operation,
                "revision": revision,
                "attempts": prior + 1,
                "status": "started",
                "intent_id": uuid4().hex,
                "started_at": _now(),
                "metadata": metadata or {},
            }
            encoded = json.dumps(record["metadata"], separators=(",", ":"), sort_keys=True)
            db.execute(
                """INSERT INTO operation_budgets(namespace, attempts) VALUES(?,1)
                ON CONFLICT(namespace) DO UPDATE SET attempts=attempts+1""",
                (self.namespace,),
            )
            db.execute(
                """INSERT INTO operation_current
                (namespace, operation, revision, attempts, status, intent_id, started_at, metadata)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(namespace, operation, revision) DO UPDATE SET
                attempts=excluded.attempts, status=excluded.status, intent_id=excluded.intent_id,
                started_at=excluded.started_at, finished_at=NULL,
                metadata=excluded.metadata, outcome=NULL""",
                (
                    self.namespace,
                    operation,
                    revision,
                    record["attempts"],
                    "started",
                    record["intent_id"],
                    record["started_at"],
                    encoded,
                ),
            )
            db.execute(
                """INSERT INTO operation_history
                (namespace, operation, revision, attempts, status, intent_id, started_at, metadata,
                 event, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    self.namespace,
                    operation,
                    revision,
                    record["attempts"],
                    "started",
                    record["intent_id"],
                    record["started_at"],
                    encoded,
                    "started",
                    _now(),
                ),
            )
            return record

    def finish(
        self,
        operation: str,
        revision: str,
        *,
        succeeded: bool,
        outcome: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        operation, revision = self._key(operation, revision)
        with transaction(self.database) as db:
            row = db.execute(
                "SELECT * FROM operation_current WHERE namespace=? AND operation=? AND revision=?",
                (self.namespace, operation, revision),
            ).fetchone()
            if not row:
                raise KeyError(f"operation was not started: {operation}@{revision}")
            status, finished = ("succeeded" if succeeded else "failed"), _now()
            encoded = (
                json.dumps(outcome, separators=(",", ":"), sort_keys=True)
                if outcome is not None
                else row["outcome"]
            )
            db.execute(
                "UPDATE operation_current SET status=?, finished_at=?, outcome=? "
                "WHERE namespace=? AND operation=? AND revision=?",
                (status, finished, encoded, self.namespace, operation, revision),
            )
            db.execute(
                """INSERT INTO operation_history
                (namespace, operation, revision, attempts, status, intent_id, started_at,
                 finished_at,
                 metadata, outcome, event, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    self.namespace,
                    operation,
                    revision,
                    row["attempts"],
                    status,
                    row["intent_id"],
                    row["started_at"],
                    finished,
                    row["metadata"],
                    encoded,
                    status,
                    finished,
                ),
            )
            current = dict(row)
            current.update(status=status, finished_at=finished, outcome=encoded)
            return self._record(current)

    def recoverable(self) -> list[dict[str, Any]]:
        with connect(self.database) as db:
            rows = db.execute(
                "SELECT * FROM operation_current WHERE namespace=? AND status='started' "
                "ORDER BY operation,revision",
                (self.namespace,),
            ).fetchall()
        return [self._record(row) for row in rows]

    def get(self, operation: str, revision: str) -> dict[str, Any] | None:
        operation, revision = self._key(operation, revision)
        with connect(self.database) as db:
            row = db.execute(
                "SELECT * FROM operation_current WHERE namespace=? AND operation=? AND revision=?",
                (self.namespace, operation, revision),
            ).fetchone()
        return self._record(row) if row else None
