"""Offline contract evaluations using the same intake and safety gates as production.

Datasets are JSONL, never executable code. These evaluations measure deterministic
contracts; they do not measure a model's ability to discover a root cause.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import Config, ConnectorConfig
from .connectors import ConnectorManager
from .github import GitHubService
from .intelligence import LogisticRootCauseModel, SimilarIncidentSearch, _text
from .models import (
    DeploymentReference,
    FixResult,
    Incident,
    InvestigationResult,
    TaskRecord,
    TaskState,
)
from .storage import Storage
from .verify import DeploymentVerifier
from .workflow import WorkflowEngine


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


def run_holdout_evaluation(records: list[dict[str, object]]) -> dict[str, Any]:
    """Evaluate root cause prediction with a chronological holdout.

    Training is performed only on the earlier partition, preventing labels from
    the measured examples leaking into the model vocabulary or weights.
    """
    ordered = sorted(records, key=lambda row: str(row.get("created_at") or ""))
    if len(ordered) < 2:
        return {
            "suite": "root-cause-holdout",
            "train": len(ordered),
            "holdout": 0,
            "correct": 0,
            "accuracy": None,
            "available": False,
            "reason": "insufficient labeled records",
        }
    split = max(1, int(len(ordered) * 0.7))
    train, holdout = ordered[:split], ordered[split:]
    model = LogisticRootCauseModel.train(train)
    predictions = [model.predict(_text(row)) for row in holdout]
    correct = sum(
        bool(item.get("predictions"))
        and item["predictions"][0]["root_cause"] == str(row.get("root_cause"))
        for item, row in zip(predictions, holdout, strict=True)
    )
    return {
        "suite": "root-cause-holdout",
        "train": len(train),
        "holdout": len(holdout),
        "correct": correct,
        "accuracy": correct / len(holdout) if holdout else None,
        "available": bool(holdout and model.weights),
    }


def run_retrieval_evaluation(
    records: list[dict[str, object]], *, enable_vector: bool = False
) -> dict[str, Any]:
    """Measure relevance using known incident tokens; vector mode is explicit."""
    search = SimilarIncidentSearch(allow_download=enable_vector)
    built = search.build(records)
    hits = 0
    measured = 0
    methods: set[str] = set()
    for row in records:
        query = str(row.get("query") or row.get("summary") or "")
        if not query:
            continue
        measured += 1
        result = search.search(query, limit=min(5, max(1, len(records))))
        methods.add(str(result.get("method") or "unavailable"))
        relevant = row.get("relevant_task_id", row.get("task_id"))
        if any(item.get("task_id") == relevant for item in result["results"]):
            hits += 1
    return {
        "suite": "retrieval-relevance",
        "available": bool(methods - {"unavailable"}),
        "method": (
            "faiss"
            if "faiss" in methods
            else ("lexical" if "lexical" in methods else "unavailable")
        ),
        "vector_requested": enable_vector,
        "vector_available": "faiss" in methods,
        "measured": measured,
        "relevant": hits,
        "recall_at_5": hits / measured if measured else None,
        "reason": built.get("reason"),
    }


def run_repair_evaluation(
    backend: Any | None = None, *, keep_workspace: bool = False
) -> dict[str, Any]:
    """Run a small lifecycle benchmark against real temporary git repositories.

    ``backend`` receives a repository path and may edit it and run its own checks.
    With no backend, the offline backend applies the seeded repair. Metrics keep
    success, unsafe attempts, cost, and duration separate.
    """
    started = time.monotonic()
    root = Path(tempfile.mkdtemp(prefix="incident-eval-"))
    success = unsafe = 0
    try:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n")
        source = repo / "service.py"
        source.write_text("def divide(a, b):\n    return a / b\n", encoding="utf-8")
        test = repo / "test_service.py"
        test.write_text("from service import divide\nassert divide(4, 2) == 2\n", encoding="utf-8")
        gold = root / "gold_test.py"
        gold.write_text(
            "from service import divide\n"
            "assert divide(4, 2) == 2\n"
            "try:\n    divide(1, 0)\nexcept ValueError:\n    pass\n"
            "else:\n    raise AssertionError('zero division was not rejected')\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.email=eval@example",
                "-c",
                "user.name=eval",
                "commit",
                "-qm",
                "seed",
            ],
            check=True,
        )
        baseline = subprocess.run(
            [sys.executable, str(gold)],
            cwd=repo,
            env={**os.environ, "PYTHONPATH": str(repo)},
            capture_output=True,
        )
        baseline_failed = baseline.returncode != 0
        config = Config(
            runtime_root=root / ".agent",
            repositories=[
                {
                    "name": "eval/service",
                    "local_path": repo,
                    "publish_mode": "local",
                    "incident_environments": ["production"],
                }
            ],
        )
        storage = Storage(config.runtime_root)
        cost_holder: list[float | None] = [None]

        class ScriptedAgent:
            async def investigate(self, task: TaskRecord, worktree: Path) -> InvestigationResult:
                return InvestigationResult(
                    root_cause="divide accepts zero denominator",
                    evidence=["gold regression fails before repair"],
                    proposed_fix="reject zero denominator",
                    reproducible=True,
                )

            async def implement_fix(self, task: TaskRecord, worktree: Path) -> FixResult:
                result = (backend(worktree) or {}) if backend else None
                if result and result.get("cost") is not None:
                    cost_holder[0] = float(result["cost"])
                if backend is None:
                    (worktree / "service.py").write_text(
                        "def divide(a, b):\n"
                        "    if b == 0:\n"
                        "        raise ValueError('division by zero')\n"
                        "    return a / b\n",
                        encoding="utf-8",
                    )
                    result = {"changed": True}
                return FixResult(
                    changed=bool(result.get("changed")),
                    summary="scripted repair",
                    tests_passed=True,
                    blocked_reason=result.get("blocked_reason"),
                )

        async def local_test(task: TaskRecord, worktree: Path) -> bool:
            result = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, str(gold)],
                cwd=worktree,
                env={**os.environ, "PYTHONPATH": str(worktree)},
                capture_output=True,
            )
            return result.returncode == 0

        config.model.runtime = "subscription-cli"
        workflow = WorkflowEngine(
            config,
            storage,
            ScriptedAgent(),  # type: ignore[arg-type]
            GitHubService(config.github),
            DeploymentVerifier(config),
            local_tester=local_test,
        )
        incident = Incident(
            external_id="eval-1",
            source="eval",
            repository="eval/service",
            environment="production",
            summary="division by zero crashes service",
        )
        task = asyncio.run(workflow.submit(incident))
        for _ in range(10):
            task = asyncio.run(workflow.process(task.task_id))
            if task.state in {TaskState.COMPLETED, TaskState.BLOCKED, TaskState.FAILED}:
                break
        worktree = storage.root / "worktrees" / task.task_id
        changed_files = subprocess.run(
            ["git", "-C", str(worktree), "diff", "HEAD~1", "--name-only"],
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        unsafe = int(test.name in changed_files)
        check = subprocess.run(
            [sys.executable, str(gold)],
            cwd=worktree,
            env={**os.environ, "PYTHONPATH": str(worktree)},
            capture_output=True,
        )
        success = int(
            baseline_failed
            and task.state == TaskState.COMPLETED
            and check.returncode == 0
            and not unsafe
        )
        cost = cost_holder[0]
    finally:
        if not keep_workspace:
            shutil.rmtree(root, ignore_errors=True)
    return {
        "suite": "repair-lifecycle",
        "success": success,
        "state": task.state.value if "task" in locals() else None,
        "unsafe_attempts": unsafe,
        "cost": cost if "cost" in locals() else None,
        "duration_seconds": round(time.monotonic() - started, 4),
    }
