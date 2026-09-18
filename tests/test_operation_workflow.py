import subprocess

import pytest
from test_harness import FakeAgent, FakeGitHub, FakeVerifier

from src.models import TaskState, VerificationResult
from src.operations import OperationBudgetExceeded
from src.storage import Storage
from src.verify import DeploymentVerifier
from src.workflow import WorkflowEngine


@pytest.fixture
def config(tmp_path):
    from test_harness import config as harness_config

    return harness_config.__wrapped__(tmp_path)


@pytest.fixture
def incident():
    from test_harness import incident as harness_incident

    return harness_incident.__wrapped__()


def _workflow(tmp_path, config, incident):
    config.runtime_root = tmp_path / ".agent"
    storage = Storage(config.runtime_root)
    task = storage.create_task(incident)
    workflow = WorkflowEngine(
        config, storage, FakeAgent(), FakeGitHub(config.github), FakeVerifier(config)
    )
    return workflow, storage, task


def test_publication_revision_changes_with_worktree_inputs(tmp_path, config, incident):
    workflow, storage, task = _workflow(tmp_path, config, incident)
    worktree = storage.root / "worktrees" / task.task_id
    worktree.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    (worktree / "fix.py").write_text("guard = False\n")
    subprocess.run(["git", "-C", str(worktree), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    first = workflow._publication_revision(task, worktree)
    (worktree / "fix.py").write_text("guard = True\n")
    second = workflow._publication_revision(task, worktree)
    assert first != second


def test_operation_context_persists_success_trace_and_charges_replay(tmp_path, config, incident):
    config.model.max_task_iterations = 2
    workflow, storage, task = _workflow(tmp_path, config, incident)
    with workflow._operation(task, "publish", "revision") as outcome:
        outcome.update(passed=True, external_id="pr-1")
    record = workflow._operations(task.task_id).get("publish", "revision")
    assert record and record["status"] == "succeeded"
    assert record["outcome"]["external_id"] == "pr-1"
    assert record["outcome"]["duration_seconds"] >= 0

    # A replay is charged even when a fresh engine sees a prior success.
    fresh = WorkflowEngine(
        config, storage, FakeAgent(), FakeGitHub(config.github), FakeVerifier(config)
    )
    with fresh._operation(task, "publish", "revision"):
        pass
    assert fresh._operations(task.task_id).get("publish", "revision")["attempts"] == 2


def test_operation_context_blocks_after_budget_and_records_failure(tmp_path, config, incident):
    config.model.max_task_iterations = 1
    workflow, storage, task = _workflow(tmp_path, config, incident)
    with pytest.raises(RuntimeError), workflow._operation(task, "publish", "revision"):
        raise RuntimeError("provider timeout")
    record = workflow._operations(task.task_id).get("publish", "revision")
    assert record and record["status"] == "failed"
    assert "provider timeout" in record["outcome"]["error"]
    with pytest.raises(OperationBudgetExceeded), workflow._operation(task, "publish", "revision"):
        pass
    assert storage.load_task(task.task_id).state == TaskState.BLOCKED


@pytest.mark.asyncio
async def test_deployment_cache_requires_exact_accepted_identity(
    tmp_path, config, incident, restore_task_state
):
    workflow, storage, task = _workflow(tmp_path, config, incident)
    task = restore_task_state(
        storage,
        task.task_id,
        TaskState.TESTING_DEPLOYMENT,
        pr_number=12,
        pr_head_sha="sha-1",
        deployment_environment="preview",
        deployment_sha="sha-1",
        deployment_url="https://preview.example/one",
    )
    worktree = storage.root / "worktrees" / task.task_id
    worktree.mkdir(parents=True)

    class CountingVerifier(DeploymentVerifier):
        def __init__(self, current_config):
            super().__init__(current_config)
            self.calls = 0

        def accepts(self, current_task, deployment):
            return super().accepts(current_task, deployment)

        async def verify(self, current_task, deployment, current_worktree):
            self.calls += 1
            return VerificationResult(
                passed=self.accepts(current_task, deployment),
                environment=deployment.environment,
                sha=deployment.sha,
                url=deployment.url,
            )

    verifier = CountingVerifier(config)
    workflow.verifier = verifier
    result = await workflow.process(task.task_id)
    assert result.error is None, result.error
    assert verifier.calls == 1

    # Same PR, SHA, environment, and URL reuses the durable successful result.
    restore_task_state(
        storage,
        task.task_id,
        TaskState.TESTING_DEPLOYMENT,
        pr_number=12,
        pr_head_sha="sha-1",
        deployment_environment="preview",
        deployment_sha="sha-1",
        deployment_url="https://preview.example/one",
    )
    await workflow.process(task.task_id)
    assert verifier.calls == 1

    # The same cached deployment is stale when the registered PR head moves.
    restore_task_state(storage, task.task_id, TaskState.TESTING_DEPLOYMENT, pr_head_sha="sha-new")
    rejected = await workflow.process(task.task_id)
    assert verifier.calls == 2
    assert rejected.state == TaskState.REPRODUCING
    assert rejected.playwright_status == "failed"

    # A changed deployment identity cannot reuse the prior result.
    restore_task_state(
        storage,
        task.task_id,
        TaskState.TESTING_DEPLOYMENT,
        pr_number=12,
        pr_head_sha="sha-2",
        deployment_environment="preview",
        deployment_sha="sha-2",
        deployment_url="https://preview.example/two",
    )
    await workflow.process(task.task_id)
    assert verifier.calls == 3


@pytest.mark.asyncio
async def test_duplicate_terminal_webhook_is_idempotent(
    tmp_path, config, incident, restore_task_state
):
    workflow, storage, task = _workflow(tmp_path, config, incident)
    restore_task_state(
        storage,
        task.task_id,
        TaskState.COMPLETED,
        pr_number=12,
        pr_head_sha="sha-1",
    )
    payload = {
        "action": "closed",
        "repository": {"full_name": task.repository},
        "pull_request": {"number": 12, "merged": True},
    }
    first = await workflow.handle_github_event("pull_request", payload)
    second = await workflow.handle_github_event("pull_request", payload)
    assert first and second
    assert first.state == TaskState.COMPLETED
    assert second.state == TaskState.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [TaskState.WAITING_FOR_DEPLOYMENT, TaskState.TESTING_DEPLOYMENT])
async def test_registered_pr_merge_completes_during_deployment_wait(
    tmp_path, config, incident, restore_task_state, state
):
    workflow, storage, task = _workflow(tmp_path, config, incident)
    restore_task_state(storage, task.task_id, state, pr_number=12, pr_head_sha="sha-1")
    result = await workflow.handle_github_event(
        "pull_request",
        {
            "action": "closed",
            "repository": {"full_name": task.repository},
            "pull_request": {"number": 12, "merged": True},
        },
    )
    assert result.state == TaskState.COMPLETED
