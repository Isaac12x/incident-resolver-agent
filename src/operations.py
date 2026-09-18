"""File-backed operation attempts and recovery checkpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .file_store import JsonFile


class OperationBudgetExceeded(RuntimeError):
    """The operation or task-wide retry budget is exhausted."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class OperationLedger:
    """Durably checkpoint side-effecting operations.

    Records are keyed by logical operation and input revision. A revision change
    gets its own attempt budget, but all revisions share the overall cap.
    """

    def __init__(self, path: Path | str, *, max_attempts: int = 3, overall_cap: int = 10) -> None:
        if max_attempts < 1 or overall_cap < 1:
            raise ValueError("operation budgets must be positive")
        self.store = JsonFile(Path(path), lambda: {"version": 1, "attempts": 0, "operations": {}})
        self.max_attempts = max_attempts
        self.overall_cap = overall_cap

    def _key(self, operation: str, revision: str) -> str:
        if not operation.strip() or not revision.strip():
            raise ValueError("operation and revision are required")
        return f"{operation}\0{revision}"

    def begin(
        self,
        operation: str,
        revision: str,
        *,
        metadata: dict[str, Any] | None = None,
        reconcile: bool = False,
        reuse_success: bool = True,
    ) -> dict[str, Any]:
        key = self._key(operation, revision)
        with self.store.transaction() as data:
            record = data.setdefault("operations", {}).get(key)
            if record and record.get("status") == "succeeded" and reuse_success:
                return dict(record)
            if record and record.get("status") == "started" and reconcile:
                # Reconciliation can inspect an intent without replaying its effect.
                return dict(record)
            total = int(data.get("attempts", 0))
            prior = int(record.get("attempts", 0)) if record else 0
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
            data["attempts"] = total + 1
            data["operations"][key] = record
            data.setdefault("history", []).append({**record, "event": "started"})
            return dict(record)

    def finish(
        self,
        operation: str,
        revision: str,
        *,
        succeeded: bool,
        outcome: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        key = self._key(operation, revision)
        with self.store.transaction() as data:
            record = data.setdefault("operations", {}).get(key)
            if not record:
                raise KeyError(f"operation was not started: {operation}@{revision}")
            record["status"] = "succeeded" if succeeded else "failed"
            record["finished_at"] = _now()
            if outcome is not None:
                record["outcome"] = outcome
            data.setdefault("history", []).append({**record, "event": record["status"]})
            return dict(record)

    def recoverable(self) -> list[dict[str, Any]]:
        return [
            dict(record)
            for record in self.store.read().get("operations", {}).values()
            if record.get("status") == "started"
        ]

    def get(self, operation: str, revision: str) -> dict[str, Any] | None:
        return self.store.read().get("operations", {}).get(self._key(operation, revision))
