from __future__ import annotations

import subprocess

import pytest

from src.config import Config, RepositoryConfig
from src.github import GitHubService
from src.models import Incident, TaskState
from src.storage import Storage
from src.workflow import WorkflowEngine


def test_conflict_recovery_requires_explicit_dirty_mergeability() -> None:
    base = {"pull_request": {"mergeable_state": "unknown"}}
    assert not WorkflowEngine._is_conflicted_pull_request(base)
    assert not WorkflowEngine._is_conflicted_pull_request(
        {"pull_request": {"mergeable": False, "mergeable_state": "blocked"}}
    )
    assert WorkflowEngine._is_conflicted_pull_request(
        {"pull_request": {"mergeable_state": "dirty", "head": {"sha": "head"}}}
    )


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.asyncio
async def test_recovery_merges_actual_base_and_leaves_conflict_for_session(tmp_path):
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
            external_id="INC-CONFLICT",
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
    result = await engine.handle_github_event("pull_request", payload)
    assert result.conflict_pending
    assert result.conflict_base_branch == "main"
    assert result.conflict_recovery_attempts == 1
    assert git(worktree, "diff", "--name-only", "--diff-filter=U") == "value.txt"
    crashed = storage.load_task(task.task_id).model_copy(
        update={"conflict_pending": False, "conflict_merge_pending": True}
    )
    storage.save_task(crashed)
    engine.config.model.max_task_iterations = 1
    resumed = await engine._recover_pull_request_conflict(
        crashed,
        ("org/repo", 7),
        {"pull_request": {"base": {"ref": "main", "repo": {"full_name": "org/repo"}}}},
    )
    assert resumed.conflict_recovery_attempts == 1
