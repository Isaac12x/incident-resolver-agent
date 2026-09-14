"""OCR setup, artifact persistence and pre-Playwright gate regressions."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import AsyncMock, Mock

import pytest
from textual.widgets import Checkbox, Input

from src import code_review
from src.agent import IncidentAgent
from src.config import CodeReviewConfig, Config, RepositoryConfig, load_config, save_config
from src.models import Incident, PullRequestReference, TaskState, VerificationResult
from src.storage import Storage
from src.tui import ConfigurationApp
from src.workflow import WorkflowEngine, _TaskLifecycle


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_OCR_KEY", "test-secret-value")
    return Config(
        runtime_root=tmp_path / "runtime",
        code_review=CodeReviewConfig(
            enabled=True,
            model="review-model",
            api_key_env="TEST_OCR_KEY",
        ),
        repositories=[RepositoryConfig(name="owner/repo")],
    )


def test_configuration_and_environment(configured, tmp_path, monkeypatch):
    save_config(configured, tmp_path / "config.toml")
    assert load_config(tmp_path / "config.toml") == configured
    assert "test-secret-value" not in (tmp_path / "config.toml").read_text()
    monkeypatch.setenv("OCR_LLM_EXTRA_HEADERS", "unwanted")
    env = code_review.environment(configured)
    assert env["OCR_LLM_TOKEN"] == "test-secret-value"
    assert "OCR_LLM_EXTRA_HEADERS" not in env
    with pytest.raises(ValueError):
        CodeReviewConfig(enabled=True)
    monkeypatch.delenv("TEST_OCR_KEY")
    with pytest.raises(ValueError, match="environment variable"):
        code_review.environment(configured)


def test_provision_validates_before_installing(configured, monkeypatch):
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(code_review.shutil, "which", lambda _: None)
    assert "connection passed" in code_review.provision(configured, runner)
    assert runner.call_args_list[0].args[0][:2] == ["npm", "install"]
    assert runner.call_args_list[1].args[0][-2:] == ["llm", "test"]
    assert runner.call_args_list[1].kwargs["env"]["OCR_LLM_MODEL"] == "review-model"
    runner.reset_mock()
    configured.permissions.allow_dependency_installation = False
    with pytest.raises(ValueError, match="installation"):
        code_review.provision(configured, runner)
    runner.assert_not_called()
    configured.code_review.enabled = False
    with pytest.raises(ValueError, match="Enable"):
        code_review.provision(configured, runner)


def test_provision_existing_and_errors(configured, monkeypatch):
    local = configured.runtime_root / "tools/ocr/bin/ocr"
    local.parent.mkdir(parents=True)
    local.touch()
    assert code_review.executable(configured) == str(local)
    runner = Mock(return_value=subprocess.CompletedProcess([], 1, "secret", "secret"))
    with pytest.raises(RuntimeError, match="connection"):
        code_review.provision(configured, runner)
    local.unlink()
    monkeypatch.setattr(code_review.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="installation"):
        code_review.provision(configured, runner)


@pytest.mark.parametrize(
    "report,exitcode,error",
    [
        ({"status": "complete", "comments": []}, 0, None),
        ({"status": "complete", "comments": None}, 0, None),
        ({"status": "complete", "comments": [], "summary": "invalid"}, 0, ValueError),
        ({"status": "success", "comments": [{"body": "test-secret-value"}]}, 0, None),
        ({"status": "partial", "comments": []}, 0, ValueError),
        ({"status": "complete"}, 0, ValueError),
        ({"status": "complete", "comments": [], "warnings": ["error"]}, 0, ValueError),
        (
            {"status": "complete", "comments": [], "summary": {"budget_exceeded": True}},
            0,
            ValueError,
        ),
        ({"status": "complete", "comments": []}, 1, RuntimeError),
    ],
)
def test_review_contract(configured, tmp_path, monkeypatch, report, exitcode, error):
    output = tmp_path / "scan-result.json"
    output.write_text("stale")

    def run(command, **kwargs):
        assert not output.exists()
        assert command[1:] == [
            "review",
            "--from",
            "main",
            "--to",
            "feature-branch",
            "--format",
            "json",
            "--output",
            str(output),
        ]
        output.write_text(json.dumps(report))
        return subprocess.CompletedProcess(command, exitcode, "", "")

    monkeypatch.setattr(code_review.subprocess, "run", run)
    if error:
        with pytest.raises(error):
            code_review.review(configured, tmp_path, "main", "feature-branch", output)
    else:
        result = code_review.review(configured, tmp_path, "main", "feature-branch", output)
        assert result["status"] == report["status"]
        assert "test-secret-value" not in output.read_text()


@pytest.fixture
def workflow(configured):
    storage = Storage(configured.runtime_root)
    task = storage.create_task(
        Incident(
            external_id="1",
            source="test",
            repository="owner/repo",
            environment="production",
            summary="broken",
        )
    )
    worktree = storage.root / "worktrees" / task.task_id
    worktree.mkdir(parents=True)
    for args in (
        ["init", "-b", "main"],
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@local",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
        ],
        ["checkout", "-b", "feature-branch"],
    ):
        subprocess.run(["git", *args], cwd=worktree, check=True, capture_output=True)
    task.branch = "feature-branch"
    storage.save_task(task)
    task = storage.transition(task.task_id, TaskState.TESTING_LOCAL)
    engine = WorkflowEngine(
        configured, storage, Mock(supports_durable_session=False), Mock(), Mock(verify=AsyncMock())
    )
    return engine, task, worktree


@pytest.mark.asyncio
async def test_findings_return_to_fix_before_playwright(workflow, monkeypatch):
    engine, task, worktree = workflow
    report = {"status": "complete", "comments": [{"body": "Fix the boundary"}]}

    def scan(config, cwd, base, branch, output):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report))
        return report

    monkeypatch.setattr("src.workflow.review", scan)
    result = await _TaskLifecycle(engine, task.task_id, worktree).run_tests("npx playwright test")
    assert result["state"] == TaskState.REPRODUCING
    assert "Fix the boundary" in result["review_report"]
    engine.verifier.verify.assert_not_called()
    latest = engine.storage.load_task(task.task_id)
    agent = IncidentAgent(engine.config, engine.storage, Mock())
    assert "Fix the boundary" in agent._instructions(latest, worktree, ())
    directory = engine.storage.task_directory(task.task_id) / "artifacts/code-review"
    assert list(directory.glob("*.json"))
    latest.attempts = engine.config.model.max_task_iterations - 1
    engine.storage.save_task(latest)
    assert not await engine._review_fix(latest, worktree)
    assert engine.storage.load_task(task.task_id).state == TaskState.BLOCKED


@pytest.mark.asyncio
async def test_clean_review_cache_and_new_commit(workflow, monkeypatch):
    engine, task, worktree = workflow
    scan = Mock(return_value={"status": "complete", "comments": []})
    monkeypatch.setattr("src.workflow.review", scan)
    (worktree / "fix.py").write_text("fixed = True\n")
    assert await engine._review_fix(task, worktree)
    task = engine.storage.load_task(task.task_id)
    assert task.code_review_sha
    assert await engine._review_fix(task, worktree)
    assert scan.call_count == 1
    (worktree / "fix.py").write_text("fixed = False\n")
    assert await engine._review_fix(task, worktree)
    assert scan.call_count == 2
    task = engine.storage.load_task(task.task_id)
    task.code_review_sha = None
    engine.storage.save_task(task)
    scan.side_effect = RuntimeError("OCR failed")
    assert not await engine._review_fix(task, worktree)
    assert engine.storage.load_task(task.task_id).state == TaskState.BLOCKED


@pytest.mark.asyncio
async def test_deployment_never_runs_with_stale_review(workflow, monkeypatch):
    engine, task, worktree = workflow
    engine.storage.transition(task.task_id, TaskState.TESTING_DEPLOYMENT, pr_head_sha="stale")
    result = await engine.process(task.task_id)
    assert result.state == TaskState.BLOCKED
    engine.verifier.verify.assert_not_called()


@pytest.mark.asyncio
async def test_tui_saves_before_provisioning(configured, tmp_path, monkeypatch):
    configured.repositories[0].local_path = tmp_path
    path = tmp_path / "config.toml"
    save_config(configured, path)
    seen = []

    def setup(draft, runner):
        assert load_config(path) == draft
        seen.append(draft.code_review.model)
        return "Ready"

    monkeypatch.setattr("src.tui.provision", setup)
    app = ConfigurationApp(path)
    async with app.run_test(size=(120, 60)):
        app.query_one("#ocr-model", Input).value = "new-review-model"
        app.query_one("#ocr-enabled", Checkbox).value = True
        await app._setup_code_review()
        assert seen == ["new-review-model"]
        app.query_one("#ocr-model", Input).value = ""
        await app._setup_code_review()
        assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", [TaskState.TESTING_LOCAL, TaskState.PUBLISHING_PR, TaskState.TESTING_DEPLOYMENT]
)
async def test_workflow_gate_findings(workflow, monkeypatch, state):
    engine, task, worktree = workflow
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=worktree, text=True).strip()
    engine.storage.transition(task.task_id, state, pr_head_sha=head)
    monkeypatch.setattr(
        "src.workflow.review",
        Mock(
            return_value={
                "status": "complete",
                "comments": [{"body": "repair"}],
            }
        ),
    )
    result = await engine.process(task.task_id)
    assert result.state == TaskState.REPRODUCING
    engine.verifier.verify.assert_not_called()


@pytest.mark.asyncio
async def test_open_pr_returns_findings(workflow, monkeypatch):
    engine, task, worktree = workflow
    monkeypatch.setattr(
        "src.workflow.review",
        Mock(
            return_value={
                "status": "complete",
                "comments": [{"body": "repair"}],
            }
        ),
    )
    result = await _TaskLifecycle(engine, task.task_id, worktree).open_pr("fix")
    assert not result["passed"]
    assert result["state"] == TaskState.REPRODUCING


@pytest.mark.asyncio
async def test_review_ref_validation(workflow, monkeypatch):
    engine, task, worktree = workflow
    scan = Mock(return_value={"status": "complete", "comments": []})
    monkeypatch.setattr("src.workflow.review", scan)
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "main"], cwd=worktree, check=True
    )
    subprocess.run(["git", "branch", "-D", "main"], cwd=worktree, check=True, capture_output=True)
    assert await engine._review_fix(task, worktree)
    assert scan.call_args.args[2] == "origin/main"
    task.branch = "wrong-branch"
    assert not await engine._review_fix(task, worktree)
    assert "feature branch" in engine.storage.load_task(task.task_id).error


@pytest.mark.asyncio
async def test_deployment_dirty_and_review_changed_head(workflow, monkeypatch):
    engine, task, worktree = workflow
    (worktree / "dirty.py").write_text("dirty = True")
    task.state = TaskState.TESTING_DEPLOYMENT
    assert not await engine._review_fix(task, worktree)
    assert "worktree changed" in engine.storage.load_task(task.task_id).error
    task.state = TaskState.TESTING_LOCAL

    def scan(*args):
        engine.storage.commit_worktree(task, "concurrent edit")
        return {"status": "complete", "comments": []}

    monkeypatch.setattr("src.workflow.review", scan)
    assert not await engine._review_fix(task, worktree)
    assert "changed during" in engine.storage.load_task(task.task_id).error


@pytest.mark.asyncio
async def test_review_repairs_republish_then_verify(workflow, monkeypatch):
    engine, task, worktree = workflow
    engine.storage.transition(task.task_id, TaskState.TESTING_LOCAL, pr_number=7)
    monkeypatch.setattr(
        "src.workflow.review",
        Mock(
            return_value={
                "status": "complete",
                "comments": [],
            }
        ),
    )
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=worktree, text=True).strip()
    engine.github.update_pull_request = AsyncMock(
        return_value=PullRequestReference(
            repository=task.repository,
            number=7,
            url="https://github.test/pr/7",
            head_sha=head,
            branch=task.branch,
        )
    )
    result = await engine.process(task.task_id)
    assert result.state == TaskState.WAITING_FOR_DEPLOYMENT
    engine.github.update_pull_request.assert_awaited_once()
    assert result.pr_head_sha == result.code_review_sha == head
    engine.storage.transition(
        task.task_id,
        TaskState.TESTING_DEPLOYMENT,
        deployment_sha=head,
        deployment_environment="preview",
        deployment_url="https://preview.test",
    )
    engine.verifier.verify.return_value = VerificationResult(
        passed=True,
        sha=head,
        environment="preview",
        url="https://preview.test",
    )
    engine.github.publish_verification = AsyncMock()
    result = await engine.process(task.task_id)
    assert result.state == TaskState.WAITING_FOR_REVIEW
    engine.verifier.verify.assert_awaited_once()


@pytest.mark.asyncio
async def test_readonly_and_uncommitted_review_mutations(workflow, monkeypatch):
    engine, task, worktree = workflow
    (worktree / "fix.py").write_text("fixed = True")
    engine.config.permissions.mode = "read-only"
    assert not await engine._review_fix(task, worktree)
    assert "read-only" in engine.storage.load_task(task.task_id).error
    engine.config.permissions.mode = "workspace"

    def scan(*args):
        (worktree / "fix.py").write_text("fixed = False")
        return {"status": "complete", "comments": []}

    monkeypatch.setattr("src.workflow.review", scan)
    assert not await engine._review_fix(task, worktree)
    assert "worktree changed during" in engine.storage.load_task(task.task_id).error


@pytest.mark.parametrize("payload", ["{", "[]", '{"status":"success","comments":[]}'])
def test_missing_invalid_and_timeout_output(configured, tmp_path, monkeypatch, payload):
    output = tmp_path / "scan-result.json"

    def run(*args, **kwargs):
        output.write_text(payload)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(code_review.subprocess, "run", run)
    if payload.startswith('{"status"'):
        assert code_review.review(configured, tmp_path, "main", "branch", output)["comments"] == []
    else:
        with pytest.raises(ValueError):
            code_review.review(configured, tmp_path, "main", "branch", output)
    monkeypatch.setattr(
        code_review.subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    )
    with pytest.raises(FileNotFoundError):
        code_review.review(configured, tmp_path, "main", "branch", output)
    monkeypatch.setattr(
        code_review.subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired("ocr", 1))
    )
    with pytest.raises(subprocess.TimeoutExpired):
        code_review.review(configured, tmp_path, "main", "branch", output)
