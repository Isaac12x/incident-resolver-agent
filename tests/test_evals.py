from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.evals import run_evaluations


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
