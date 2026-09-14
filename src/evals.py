"""Offline contract evaluations using the same intake and safety gates as production.

Datasets are JSONL, never executable code. These evaluations measure deterministic
contracts; they do not measure a model's ability to discover a root cause.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import Config, ConnectorConfig
from .connectors import ConnectorManager
from .github import GitHubService
from .models import DeploymentReference, TaskRecord
from .verify import DeploymentVerifier


class EvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    kind: Literal["intake", "deployment", "review"]
    payload: dict[str, Any]
    config: dict[str, Any] = Field(default_factory=dict)
    task: dict[str, Any] | None = None
    connector: str = "grafana"
    expected: dict[str, Any] | bool | None = None
    expected_error: Literal["ValueError", "KeyError"] | None = None

    @model_validator(mode="after")
    def has_expectation(self) -> EvaluationCase:
        if self.expected_error is not None:
            if self.expected is not None:
                raise ValueError("expected and expected_error are mutually exclusive")
        elif self.kind == "intake":
            if not isinstance(self.expected, dict) or not self.expected:
                raise ValueError("intake requires nonempty expected fields")
        elif not isinstance(self.expected, bool):
            raise ValueError("deployment/review require a boolean expected result")
        if self.kind == "deployment" and self.task is None:
            raise ValueError("deployment requires a task")
        return self


def _evaluate(case: EvaluationCase) -> dict[str, Any] | bool:
    config = Config.model_validate(case.config)
    if case.kind == "intake":
        manager = ConnectorManager([ConnectorConfig(name=case.connector, type="webhook")])
        return manager.normalize_incident(case.connector, case.payload).model_dump(mode="json")
    if case.kind == "review":
        return GitHubService(config.github).review_comment(case.payload) is not None
    task = TaskRecord.model_validate(case.task)
    deployment = DeploymentReference.model_validate(case.payload)
    return DeploymentVerifier(config).accepts(task, deployment)


def run_evaluations(dataset: Path | None = None) -> dict[str, Any]:
    """Return a versioned report; malformed datasets fail before any case is run.

    Reports omit input/output bodies to avoid copying production evidence or secrets.
    The content digest lets CI compare identical datasets across harness versions.
    """
    source = dataset or Path(__file__).with_name("eval_cases.jsonl")
    content = source.read_bytes()
    cases: list[EvaluationCase] = []
    ids: set[str] = set()
    for number, line in enumerate(content.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            case = EvaluationCase.model_validate(json.loads(line))
        except ValueError:
            raise ValueError(f"invalid evaluation case at line {number}") from None
        if case.id in ids:
            raise ValueError(f"duplicate evaluation id at line {number}")
        ids.add(case.id)
        cases.append(case)
    if not cases:
        raise ValueError("evaluation dataset is empty")
    results = []
    for case in cases:
        try:
            actual = _evaluate(case)
        except (ValueError, KeyError) as error:
            # Pydantic ValidationError is a ValueError subtype.
            category = "KeyError" if isinstance(error, KeyError) else "ValueError"
            passed = case.expected_error == category
            reason = "expected rejection" if passed else f"unexpected {category}"
        except Exception:
            passed, reason = False, "unexpected evaluation failure"
        else:
            if case.expected_error:
                passed, reason = False, "expected rejection did not occur"
            elif isinstance(case.expected, dict):
                passed = isinstance(actual, dict) and all(
                    key in actual and actual[key] == value for key, value in case.expected.items()
                )
                reason = "matched" if passed else "expected fields did not match"
            else:
                passed = actual is case.expected
                reason = "matched" if passed else "expected decision did not match"
        results.append({"id": case.id, "kind": case.kind, "passed": passed, "reason": reason})
    passed_count = sum(result["passed"] for result in results)
    return {
        "schema_version": 1,
        "suite": "offline-contracts",
        "dataset_sha256": hashlib.sha256(content).hexdigest(),
        "total": len(results),
        "passed": passed_count,
        "failed": len(results) - passed_count,
        "pass_rate": passed_count / len(results),
        "results": results,
    }
