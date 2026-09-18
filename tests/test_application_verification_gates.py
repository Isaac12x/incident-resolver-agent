from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from pathlib import Path

import pytest

from src.config import ApplicationConfig, Config, RepositoryConfig
from src.github import GitHubService
from src.models import Incident, TaskState
from src.storage import Storage
from src.verify import DeploymentVerifier
from src.workflow import WorkflowEngine, _TaskLifecycle


def _checkout(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-qb", "main", str(path)], check=True)
    (path / "area").mkdir()
    (path / "area/seed.py").write_text("value = 1\n")
    (path / "area/caller.py").write_text("value = 1\n")
    (path / "check.py").write_text("assert True\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Verification",
            "-c",
            "user.email=verification@example.test",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    return path


def _graph(root: Path) -> None:
    database = root / ".code-review-graph/graph.db"
    database.parent.mkdir(exist_ok=True)
    with sqlite3.connect(database) as db:
        db.executescript("""
            DROP TABLE IF EXISTS nodes;
            DROP TABLE IF EXISTS edges;
            CREATE TABLE nodes(kind, name, qualified_name, file_path, file_hash);
            CREATE TABLE edges(kind, source_qualified, target_qualified, file_path);
        """)
        for path in (root / "area/seed.py", root / "area/caller.py", root / "check.py"):
            db.execute(
                "INSERT INTO nodes VALUES (?, ?, ?, ?, ?)",
                (
                    "Test" if path.name == "check.py" else "Function",
                    path.stem,
                    str(path) + "::function",
                    str(path),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                ),
            )
        db.execute(
            "INSERT INTO edges VALUES (?, ?, ?, ?)",
            (
                "CALLS",
                str(root / "area/caller.py") + "::function",
                str(root / "area/seed.py") + "::function",
                str(root / "area/caller.py"),
            ),
        )


async def _application(tmp_path: Path) -> tuple[Config, Storage, WorkflowEngine, object]:
    names = ["shop/api", "shop/web"]
    config = Config(
        runtime_root=tmp_path / "runtime",
        repositories=[
            RepositoryConfig(
                name=name,
                local_path=_checkout(tmp_path / name.rsplit("/", 1)[1]),
                publish_mode="local",
                responsibility_paths=["area"],
            )
            for name in names
        ],
        applications=[ApplicationConfig(name="shop", repositories=names)],
    )
    storage = Storage(config.runtime_root)
    engine = WorkflowEngine(
        config,
        storage,
        object(),
        GitHubService(config.github),
        DeploymentVerifier(config),
    )
    task = await engine.submit(
        Incident(
            external_id="verification-gates",
            source="test",
            application="shop",
            environment="production",
            summary="application verification gates",
        )
    )
    await engine._worktree(task)
    for repository in names:
        _graph(storage.repository_worktree(task, repository))
    task = storage.load_task(task.task_id)
    task.state = TaskState.REPRODUCING
    storage.save_task(task)
    return config, storage, engine, task


@pytest.mark.asyncio
async def test_scoped_application_rings_gate_publication_until_each_member_completes(
    tmp_path: Path,
) -> None:
    _, storage, engine, task = await _application(tmp_path)
    parent = storage.repository_worktree(task)
    lifecycle = _TaskLifecycle(engine, task.task_id, parent)

    for repository in task.repository_names():
        worktree = storage.repository_worktree(task, repository)
        (worktree / "area/seed.py").write_text("value = 2\n")
        plan = await lifecycle.verification_plan(["area/seed.py"], repository)
        assert plan["phase"] == "fix_incident"
        result = await lifecycle.run_tests(
            "python check.py", ["area/seed.py", "check.py"], repository=repository
        )
        assert result["passed"]

    with pytest.raises(RuntimeError, match="complete verification"):
        await lifecycle.open_pr("premature application fix")

    for repository in task.repository_names():
        worktree = storage.repository_worktree(task, repository)
        (worktree / "area/caller.py").write_text("value = 2\n")
        # Editing a dependent file invalidates the upstream ring's snapshot;
        # establish that ring again before expanding to the caller.
        result = await lifecycle.run_tests(
            "python check.py", ["area/seed.py", "check.py"], repository=repository
        )
        assert result["passed"]
        result = await lifecycle.run_tests(
            "python check.py",
            ["area/caller.py", "check.py"],
            repository=repository,
        )
        assert result["passed"]

    published = await lifecycle.open_pr("complete application fix")
    assert published["state"] == TaskState.COMPLETED
    fresh = storage.load_task(task.task_id)
    assert all(state.pr_head_sha for state in fresh.repositories.values())


@pytest.mark.asyncio
async def test_partial_publication_resumes_and_rejects_stale_scoped_ledger(
    tmp_path: Path,
) -> None:
    _, storage, engine, task = await _application(tmp_path)
    lifecycle = _TaskLifecycle(engine, task.task_id, storage.repository_worktree(task))
    for repository in task.repository_names():
        worktree = storage.repository_worktree(task, repository)
        (worktree / "area/seed.py").write_text("value = 3\n")
        (worktree / "area/caller.py").write_text("value = 3\n")
        await lifecycle.verification_plan(["area/seed.py"], repository)
        await lifecycle.run_tests(
            "python check.py", ["area/seed.py", "check.py"], repository=repository
        )
        await lifecycle.run_tests(
            "python check.py", ["area/caller.py", "check.py"], repository=repository
        )

    stale = storage.repository_worktree(task, "shop/web") / "area/seed.py"
    stale.write_text("value = 99\n")
    with pytest.raises(RuntimeError, match="stale or missing"):
        await engine.application.publish(storage.load_task(task.task_id))
    partial = storage.load_task(task.task_id)
    assert partial.repositories["shop/api"].pr_head_sha
    assert partial.repositories["shop/web"].pr_head_sha is None

    task = storage.load_task(task.task_id)
    task.state = TaskState.TESTING_LOCAL
    storage.save_task(task)
    await lifecycle.run_tests(
        "python check.py", ["area/seed.py", "check.py"], repository="shop/web"
    )
    await lifecycle.run_tests(
        "python check.py", ["area/caller.py", "check.py"], repository="shop/web"
    )
    await engine.application.publish(storage.load_task(task.task_id))
    resumed = storage.load_task(task.task_id)
    assert resumed.repositories["shop/web"].pr_head_sha
