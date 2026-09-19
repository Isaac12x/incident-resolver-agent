from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agent import IncidentAgent
from src.config import ApplicationConfig, Config, RepositoryConfig
from src.github import GitHubService
from src.models import Incident, ReviewComment, TaskState
from src.storage import Storage
from src.workflow import WorkflowEngine, _TaskLifecycle


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _conflicted_checkout(tmp_path: Path):
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)], check=True)
    subprocess.run(["git", "init", "--initial-branch=main", str(source)], check=True)
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.test")
    (source / "value.txt").write_text("base\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "base")
    git(source, "remote", "add", "origin", str(remote))
    git(source, "push", "-q", "origin", "main")
    config = Config(
        runtime_root=tmp_path / "runtime",
        repositories=[RepositoryConfig(name="org/repo", clone_url=str(remote), base_branch="main")],
    )
    storage = Storage(config.runtime_root)
    task = storage.create_task(
        Incident(
            external_id="INC-CONFLICT-INTEGRATION",
            source="test",
            repository="org/repo",
            environment="production",
            summary="conflict",
        )
    )
    worktree = storage.create_worktree(task, str(remote))
    (worktree / "value.txt").write_text("incident\n")
    git(worktree, "add", ".")
    git(
        worktree,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "commit",
        "-qm",
        "incident",
    )
    git(worktree, "push", "-q", "origin", f"HEAD:{task.branch}")
    head = git(worktree, "rev-parse", "HEAD")
    (source / "value.txt").write_text("upstream\n")
    git(source, "commit", "-qam", "upstream")
    git(source, "push", "-q", "origin", "main")
    task = storage.load_task(task.task_id).model_copy(
        update={"state": TaskState.WAITING_FOR_DEPLOYMENT, "pr_number": 7, "pr_head_sha": head}
    )
    storage.save_task(task)
    return config, storage, task, worktree, head


@pytest.mark.asyncio
async def test_conflict_event_leaves_real_merge_for_agent_and_loads_skills(tmp_path: Path) -> None:
    config, storage, task, worktree, head = _conflicted_checkout(tmp_path)
    engine = WorkflowEngine(config, storage, object(), GitHubService(config.github), object())
    payload = {
        "action": "synchronize",
        "repository": {"full_name": "org/repo"},
        "pull_request": {
            "number": 7,
            "head": {"sha": head},
            "base": {"ref": "main", "repo": {"full_name": "org/repo"}},
            "mergeable_state": "dirty",
        },
    }

    recovered = await engine.handle_github_event("pull_request", payload)
    assert recovered.conflict_pending
    assert recovered.conflict_recovery_attempts == 1
    assert git(worktree, "diff", "--name-only", "--diff-filter=U") == "value.txt"
    repeated = await engine.handle_github_event("pull_request", payload)
    assert repeated.conflict_recovery_attempts == 1

    captured: dict[str, str] = {}

    async def backend(instructions, prompt, *_args, **_kwargs):  # noqa: ANN001
        captured["instructions"] = instructions
        captured["prompt"] = prompt
        return {"changed": False, "summary": "conflict reviewed", "tests_passed": True}

    async def tools_for(_capabilities):  # noqa: ANN001
        return []

    connectors = SimpleNamespace(tools_for=tools_for)
    agent = IncidentAgent(config, storage, connectors, backend)
    await agent.address_review(
        storage.load_task(task.task_id),
        [ReviewComment(id=1, body="resolve this conflict", author="reviewer")],
        worktree,
    )

    assert "resolving-merge-conflicts" in captured["instructions"]
    assert "code-review" in captured["instructions"]
    assert "Pull Request Conflict Recovery" in captured["instructions"]
    assert "resolve this conflict" in captured["prompt"]

    git(worktree, "checkout", "--theirs", "value.txt")
    git(worktree, "add", "value.txt")
    git(
        worktree,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "commit",
        "-qm",
        "resolve conflict",
    )
    verification = await _TaskLifecycle(engine, task.task_id, worktree).run_tests(
        "git diff --check", ["value.txt"]
    )
    assert verification["passed"]
    assert git(worktree, "diff", "--name-only", "--diff-filter=U") == ""
    current = storage.load_task(task.task_id).model_copy(
        update={
            "pr_number": 7,
            "conflict_pending": True,
            "conflict_merge_pending": False,
            "conflict_base_branch": "main",
        }
    )
    storage.save_task(current)
    published = await _TaskLifecycle(engine, task.task_id, worktree).open_pr(
        "Resolved conflict and reran verification."
    )
    final = storage.load_task(task.task_id)
    assert published["state"] == TaskState.WAITING_FOR_DEPLOYMENT.value
    assert not final.conflict_pending
    assert not final.conflict_merge_pending
    assert git(worktree, "ls-remote", "origin", f"refs/heads/{final.branch}").split()[0] == git(
        worktree, "rev-parse", "HEAD"
    )


@pytest.mark.asyncio
async def test_stale_conflict_webhook_does_not_touch_checkout(tmp_path: Path) -> None:
    config, storage, task, worktree, head = _conflicted_checkout(tmp_path)
    engine = WorkflowEngine(config, storage, object(), GitHubService(config.github), object())
    payload = {
        "action": "synchronize",
        "repository": {"full_name": "org/repo"},
        "pull_request": {
            "number": 7,
            "head": {"sha": "different-head"},
            "base": {"ref": "main", "repo": {"full_name": "org/repo"}},
            "mergeable_state": "dirty",
        },
    }

    result = await engine.handle_github_event("pull_request", payload)

    assert not result.conflict_pending
    assert git(worktree, "diff", "--name-only", "--diff-filter=U") == ""


@pytest.mark.asyncio
async def test_restart_after_clean_base_merge_resumes_instead_of_marking_stale(
    tmp_path: Path,
) -> None:
    config, storage, task, worktree, head = _conflicted_checkout(tmp_path)
    git(worktree, "fetch", "origin", "main")
    merge = subprocess.run(
        ["git", "-C", str(worktree), "merge", "--no-edit", "origin/main"],
        capture_output=True,
        text=True,
    )
    assert merge.returncode
    git(worktree, "checkout", "--theirs", "value.txt")
    git(worktree, "add", "value.txt")
    git(
        worktree,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "commit",
        "-qm",
        "resolved merge before restart",
    )
    task = storage.load_task(task.task_id).model_copy(
        update={
            "state": TaskState.REPRODUCING,
            "pr_number": 7,
            "pr_head_sha": head,
            "conflict_base_branch": "main",
            "conflict_merge_pending": True,
        }
    )
    storage.save_task(task)
    engine = WorkflowEngine(config, storage, object(), GitHubService(config.github), object())

    await engine._resume_pending_conflict_merge(storage.load_task(task.task_id))

    assert not any(
        event.type == "publication.conflict_stale" for event in storage.events(task.task_id)
    )


@pytest.mark.asyncio
async def test_application_member_pending_merge_is_resumed_after_restart(tmp_path: Path) -> None:
    repositories = []
    for name in ("api", "web"):
        repository = tmp_path / name
        repository.mkdir()
        git(repository, "init", "-qb", "main")
        (repository / "value.txt").write_text("base\n")
        git(repository, "add", ".")
        git(
            repository,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-qm",
            "base",
        )
        repositories.append(RepositoryConfig(name=f"shop/{name}", local_path=repository))
    config = Config(
        runtime_root=tmp_path / "runtime",
        repositories=repositories,
        applications=[
            ApplicationConfig(name="shop", repositories=["shop/api", "shop/web"])
        ],
    )
    storage = Storage(config.runtime_root)
    task = storage.create_task(
        Incident(
            external_id="INC-APP-CONFLICT-RESTART",
            source="test",
            application="shop",
            environment="production",
            summary="application conflict",
        ),
        config,
    )
    engine = WorkflowEngine(config, storage, object(), GitHubService(config.github), object())
    await engine._worktree(task)
    task = storage.load_task(task.task_id)
    task.repositories["shop/api"].conflict_merge_pending = True
    task.repositories["shop/api"].conflict_base_branch = "main"
    task.repositories["shop/api"].pr_number = 7
    storage.save_task(task)
    resumed = AsyncMock()
    engine._resume_pending_conflict_merge = resumed

    await engine.recover()

    resumed.assert_awaited_once()
