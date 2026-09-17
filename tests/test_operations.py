import pytest

from src.operations import OperationBudgetExceeded, OperationLedger


def test_operation_attempts_and_recovery_survive_new_ledger(tmp_path):
    path = tmp_path / "task" / "operations.json"
    first = OperationLedger(path, max_attempts=2, overall_cap=3)
    intent = first.begin("publish", "sha-1")
    assert intent["attempts"] == 1
    assert first.recoverable()[0]["intent_id"] == intent["intent_id"]

    # A new process must charge a replay; reconciliation is the explicit
    # read-only path for checking whether the remote effect already exists.
    second = OperationLedger(path, max_attempts=2, overall_cap=3)
    replay = second.begin("publish", "sha-1")
    assert replay["attempts"] == 2
    second.finish("publish", "sha-1", succeeded=False, outcome={"error": "timeout"})
    second.begin("publish", "sha-2")
    try:
        second.begin("publish", "sha-3")
    except OperationBudgetExceeded:
        pass
    else:
        raise AssertionError("overall cap must apply across revisions")


def test_operation_reconciliation_failure_lookup_and_argument_guards(tmp_path):
    ledger = OperationLedger(tmp_path / "operations.json", max_attempts=2, overall_cap=4)
    with pytest.raises(ValueError):
        ledger.begin("", "sha")
    with pytest.raises(ValueError):
        ledger.begin("publish", "")
    with pytest.raises(KeyError):
        ledger.finish("publish", "missing", succeeded=True)

    intent = ledger.begin("verify", "repo|prod|sha|url")
    inspected = ledger.begin("verify", "repo|prod|sha|url", reconcile=True)
    assert inspected["intent_id"] == intent["intent_id"]
    assert ledger.get("verify", "repo|prod|sha|url")["status"] == "started"
    failed = ledger.finish(
        "verify", "repo|prod|sha|url", succeeded=False, outcome={"passed": False}
    )
    assert failed["status"] == "failed"
    assert ledger.recoverable() == []

    # A failed operation can retry within its per-operation budget.
    retry = ledger.begin("verify", "repo|prod|sha|url")
    assert retry["attempts"] == 2
    ledger.finish("verify", "repo|prod|sha|url", succeeded=True)
    assert ledger.begin("verify", "repo|prod|sha|url")["status"] == "succeeded"


def test_budget_constructor_rejects_zero(tmp_path):
    with pytest.raises(ValueError):
        OperationLedger(tmp_path / "a.json", max_attempts=0)
    with pytest.raises(ValueError):
        OperationLedger(tmp_path / "b.json", overall_cap=0)
