from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.operations import OperationBudgetExceeded, OperationLedger


def legacy(path: Path, attempts: int = 2) -> None:
    record = {
        "operation": "publish",
        "revision": "sha",
        "attempts": 1,
        "status": "started",
        "intent_id": "intent",
        "started_at": "now",
        "metadata": {"source": "legacy"},
    }
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "attempts": attempts,
                "operations": {"publish\0sha": record},
                "history": [{**record, "event": "started"}],
            }
        )
    )


def test_operation_json_migration_preserves_state_and_ignores_removed_or_late_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "operations.json"
    legacy(source)
    original = source.read_bytes()
    ledger = OperationLedger(source, max_attempts=2, overall_cap=3)
    assert ledger.get("publish", "sha")["metadata"] == {"source": "legacy"}
    assert source.read_bytes() == original
    ledger.begin("publish", "other")
    with pytest.raises(OperationBudgetExceeded):
        ledger.begin("publish", "third")
    source.unlink()
    assert OperationLedger(source).get("publish", "sha")["status"] == "started"
    source.write_text("corrupt")
    assert OperationLedger(source).get("publish", "sha")["status"] == "started"


def test_operation_corrupt_source_rolls_back(tmp_path: Path) -> None:
    source = tmp_path / "operations.json"
    source.write_text(json.dumps({"version": 1, "attempts": 0, "operations": "bad", "history": []}))
    with pytest.raises(ValueError):
        OperationLedger(source)
    source.write_text(json.dumps({"version": 1, "attempts": 1, "operations": {}, "history": []}))
    assert OperationLedger(source).get("publish", "sha") is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["operations"]["publish\0sha"].update(status="unknown"),
        lambda d: d["operations"].update({"wrong": d["operations"].pop("publish\0sha")}),
        lambda d: d["operations"]["publish\0sha"].update(metadata=[]),
        lambda d: d["operations"]["publish\0sha"].update(attempts=0),
        lambda d: d.update(attempts=0),
        lambda d: d["history"].append({"operation": "x"}),
    ],
)
def test_operation_migration_rejects_invalid_records(tmp_path: Path, mutate) -> None:
    source = tmp_path / "operations.json"
    legacy(source)
    document = json.loads(source.read_text())
    mutate(document)
    source.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="invalid operation ledger"):
        OperationLedger(source)


def test_operation_namespaces_are_isolated_and_concurrent_budget_is_atomic(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    ledgers = [OperationLedger(database, namespace=name, overall_cap=1) for name in ("a", "b")]
    assert ledgers[0].begin("publish", "sha")["attempts"] == 1
    assert ledgers[1].begin("publish", "sha")["attempts"] == 1
    ledger = OperationLedger(database, namespace="concurrent", overall_cap=3)

    def begin(index: int) -> bool:
        try:
            ledger.begin("publish", str(index))
            return True
        except OperationBudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(begin, range(12)))
    assert sum(results) == 3
