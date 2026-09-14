from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.evals import (
    run_evaluations,
    run_holdout_evaluation,
    run_repair_evaluation,
    run_retrieval_evaluation,
)


def dataset(tmp_path: Path, *cases: dict) -> Path:
    path = tmp_path / "cases.jsonl"
    path.write_text("\n" + "\n".join(json.dumps(case) for case in cases))
    return path


def review(**updates) -> dict:
    return {"id": "review", "kind": "review", "payload": {}, "expected": False, **updates}


def test_shipped_contract_evaluations_are_offline_and_reproducible() -> None:
    first = run_evaluations()
    assert first == run_evaluations()
    assert first["total"] >= 12
    assert first["passed"] == first["total"]
    assert first["failed"] == 0
    assert first["suite"] == "offline-contracts"


@pytest.mark.parametrize("content", ["", "\n", "{}", "not-json", "[]"])
def test_rejects_invalid_datasets(tmp_path: Path, content: str) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(content)
    with pytest.raises(ValueError):
        run_evaluations(path)


@pytest.mark.parametrize(
    "case",
    [
        review(expected=None),
        review(expected_error="ValueError"),
        review(kind="intake", expected={}),
        review(kind="deployment"),
        review(unknown=True),
    ],
)
def test_requires_unambiguous_expectations(tmp_path: Path, case: dict) -> None:
    with pytest.raises(ValueError, match="line 2"):
        run_evaluations(dataset(tmp_path, case))


def test_duplicate_ids_and_missing_dataset_are_errors(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate"):
        run_evaluations(dataset(tmp_path, review(), review()))
    with pytest.raises(FileNotFoundError):
        run_evaluations(tmp_path / "missing")


def test_scores_actual_rejections_and_does_not_copy_sensitive_payload(tmp_path: Path) -> None:
    path = dataset(
        tmp_path,
        review(id="decision", expected=True),
        review(id="unrejected", expected=None, expected_error="ValueError"),
        review(id="rejected", config={"max_concurrent_tasks": 0}),
        {
            "id": "fields",
            "kind": "intake",
            "payload": {
                "external_id": "1",
                "repository": "r",
                "environment": "p",
                "summary": "private-production-data",
            },
            "expected": {"summary": "different"},
        },
    )
    report = run_evaluations(path)
    assert report["failed"] == 4
    assert report["pass_rate"] == 0
    assert "private-production-data" not in json.dumps(report)


@pytest.mark.parametrize(
    ("error", "expected", "passed"),
    [(KeyError("private"), "KeyError", 1), (RuntimeError("secret"), "ValueError", 0)],
)
def test_errors_are_classified_without_leaking_details(
    tmp_path: Path, error: Exception, expected: str, passed: int
) -> None:
    path = dataset(tmp_path, review(expected=None, expected_error=expected))
    with patch("src.evals._evaluate", side_effect=error):
        report = run_evaluations(path)
    assert report["passed"] == passed
    assert "private" not in json.dumps(report)
    assert "secret" not in json.dumps(report)


def test_holdout_and_retrieval_metrics_are_real_and_separate() -> None:
    records = [
        {"task_id": "1", "summary": "database timeout", "root_cause": "database"},
        {"task_id": "2", "summary": "cache miss", "root_cause": "cache"},
        {"task_id": "3", "summary": "database timeout", "root_cause": "database"},
        {"task_id": "4", "summary": "cache miss", "root_cause": "cache"},
    ]
    holdout = run_holdout_evaluation(records)
    retrieval = run_retrieval_evaluation(records)
    assert holdout["train"] + holdout["holdout"] == len(records)
    assert 0 <= retrieval["recall_at_5"] <= 1
    assert retrieval["method"] in {"lexical", "faiss", "unavailable"}


def test_holdout_rejects_insufficient_records_and_retrieval_skips_empty_queries() -> None:
    assert run_holdout_evaluation([])["available"] is False
    result = run_retrieval_evaluation([{"task_id": "1", "summary": ""}])
    assert result["measured"] == 0


def test_repair_evaluation_runs_seeded_repository_and_catches_failure() -> None:
    passed = run_repair_evaluation()
    assert passed["success"] == 1
    assert passed["unsafe_attempts"] == 0
    failed = run_repair_evaluation(
        lambda repo: {"changed": True, "tests_passed": True, "cost": 2.5}
    )
    assert failed["success"] == 0
    assert failed["cost"] == 2.5


def test_repair_eval_rejects_noop_and_test_tampering() -> None:
    assert run_repair_evaluation(lambda _: None)["success"] == 0

    def tamper(repo):
        (repo / "service.py").write_text(
            "def divide(a, b):\n    if b == 0: raise ValueError('zero')\n    return a / b\n"
        )
        (repo / "test_service.py").write_text("# removed test\n")
        return {"changed": True}

    result = run_repair_evaluation(tamper)
    assert result["success"] == 0
    assert result["unsafe_attempts"] == 1
