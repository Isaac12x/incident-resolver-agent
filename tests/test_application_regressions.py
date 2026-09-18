from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.config import ApplicationConfig, CodeReviewConfig, Config, RepositoryConfig
from src.github import GitHubService
from src.models import Incident, ReviewComment, SessionResult, TaskState
from src.storage import Storage
from src.task_catalog import TaskCatalog
from src.verify import DeploymentVerifier
from src.workflow import WorkflowEngine, _TaskLifecycle


def _checkout(path: Path, filename: str) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-qb", "main", str(path)], check=True)
    (path / filename).write_text("base\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Regression",
            "-c",
            "user.email=regression@example.test",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    return path


def _config(tmp_path: Path, *, code_review: bool = False) -> Config:
    names = ["shop/api", "shop/web"]
    return Config(
        runtime_root=tmp_path / "runtime",
        repositories=[
            RepositoryConfig(
                name=name,
                local_path=_checkout(tmp_path / name.rsplit("/", 1)[1], "value.txt"),
                publish_mode="local",
            )
            for name in names
        ],
        applications=[ApplicationConfig(name="shop", repositories=names)],
        code_review=CodeReviewConfig(enabled=code_review, model="regression-model"),
    )


async def _engine(tmp_path: Path, *, code_review: bool = False):
    config = _config(tmp_path, code_review=code_review)
    storage = Storage(config.runtime_root)
    engine = WorkflowEngine(
        config,
        storage,
        SimpleNamespace(),
        GitHubService(config.github),
        DeploymentVerifier(config),
    )
    task = await engine.submit(
        Incident(
            external_id="regression-1",
            source="test",
            application="shop",
            environment="production",
            summary="application lifecycle regression",
        )
    )
    await engine._worktree(task)
    return config, storage, engine, storage.load_task(task.task_id)


def _waiting_for_review(storage: Storage, task, comments: dict[str, ReviewComment] | None = None):
    task.state = TaskState.WAITING_FOR_REVIEW
    for repository, state in task.repositories.items():
        state.changed = True
        state.pr_number = 17
        state.pr_head_sha = subprocess.run(
            ["git", "-C", str(storage.repository_worktree(task, repository)), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if comments and repository in comments:
            state.pending_review_comments = [comments[repository]]
    storage.save_task(task)
    return storage.load_task(task.task_id)


@pytest.mark.asyncio
async def test_same_comment_number_in_each_repository_survives_failed_agent_and_restart(
    tmp_path: Path,
) -> None:
    _, storage, engine, task = await _engine(tmp_path)
    comments = {
        "shop/api": ReviewComment(id=41, body="Fix API validation", author="alice"),
        "shop/web": ReviewComment(id=41, body="Fix browser rendering", author="bob"),
    }
    task = _waiting_for_review(storage, task, comments)

    class FailingAgent:
        supports_durable_session = True

        async def run_session(self, *_args, **_kwargs):
            raise RuntimeError("agent process interrupted")

    engine.agent = FailingAgent()
    await engine.process(task.task_id)

    restarted = Storage(engine.config.runtime_root)
    recovered = restarted.load_task(task.task_id)
    assert [
        comment.body for comment in recovered.repositories["shop/api"].pending_review_comments
    ] == ["Fix API validation"]
    assert [
        comment.body for comment in recovered.repositories["shop/web"].pending_review_comments
    ] == ["Fix browser rendering"]


@pytest.mark.asyncio
async def test_review_checkpoint_preserves_comment_arriving_during_agent_call(
    tmp_path: Path,
) -> None:
    _, storage, engine, task = await _engine(tmp_path)
    original = ReviewComment(id=50, body="Original review", author="alice")
    task = _waiting_for_review(storage, task, {"shop/api": original})
    worktree = storage.root / "worktrees" / task.task_id

    class Agent:
        supports_durable_session = True

        async def run_session(self, *_args, **_kwargs):
            current = storage.load_task(task.task_id)
            current.repositories["shop/api"].pending_review_comments.append(
                ReviewComment(id=51, body="Arrived while agent ran", author="bob")
            )
            storage.save_task(current)
            return SessionResult(summary="checkpoint", waiting_for_external_event=True)

    engine.agent = Agent()
    await engine._process_agent_session(task, worktree)
    pending = storage.load_task(task.task_id).repositories["shop/api"].pending_review_comments
    assert [comment.id for comment in pending] == [51]


@pytest.mark.asyncio
async def test_second_pr_head_invalidates_a_previously_passing_deployment(tmp_path: Path) -> None:
    _, storage, engine, task = await _engine(tmp_path)
    task = _waiting_for_review(storage, task)
    for state in task.repositories.values():
        state.deployment_sha = state.pr_head_sha
        state.deployment_environment = "preview"
        state.deployment_url = "https://preview.test"
        state.playwright_status = "passed"
        state.verification_status = "passed"
        state.verification_sha = state.pr_head_sha
    storage.save_task(task)
    saved = storage.load_task(task.task_id)
    assert engine.application.all_deployments_verified(saved), (
        engine.application.changed_repositories(saved),
        [
            (name, state.changed, state.pr_head_sha, state.deployment_sha, state.playwright_status)
            for name, state in saved.repositories.items()
        ],
    )

    changed = storage.load_task(task.task_id)
    changed.repositories["shop/web"].pr_head_sha = "new-web-head"
    storage.save_task(changed)
    assert not engine.application.all_deployments_verified(storage.load_task(task.task_id))


@pytest.mark.asyncio
async def test_replaced_registered_member_fails_closed_before_resume(tmp_path: Path) -> None:
    _, storage, engine, task = await _engine(tmp_path)
    member = storage.repository_worktree(task, "shop/api")
    shutil.rmtree(member)
    member.mkdir(parents=True)
    with pytest.raises(ValueError, match="identity"):
        engine.application.verify_workspaces(storage.load_task(task.task_id))


@pytest.mark.asyncio
async def test_configured_ocr_runs_once_per_application_repository(
    tmp_path: Path, monkeypatch
) -> None:
    _, storage, engine, task = await _engine(tmp_path, code_review=True)
    task.state = TaskState.TESTING_LOCAL
    storage.save_task(task)
    calls: list[Path] = []

    def fake_review(config, worktree, base, branch, output):  # noqa: ANN001
        calls.append(Path(worktree))
        return {"comments": []}

    monkeypatch.setattr("src.workflow.review", fake_review)
    for repository in task.repository_names():
        worktree = storage.repository_worktree(task, repository)
        (worktree / "reviewed.txt").write_text(repository)
        view = engine.application.view(storage.load_task(task.task_id), repository)
        assert await engine._review_fix(view, worktree), storage.load_task(task.task_id).error

    assert calls == [
        storage.repository_worktree(task, "shop/api"),
        storage.repository_worktree(task, "shop/web"),
    ]
    reviewed = storage.load_task(task.task_id)
    assert all(state.code_review_sha for state in reviewed.repositories.values())


@pytest.mark.asyncio
async def test_application_run_tests_invokes_ocr_before_open_pr(
    tmp_path: Path, monkeypatch
) -> None:
    _, storage, engine, task = await _engine(tmp_path, code_review=True)
    task.state = TaskState.REPRODUCING
    storage.save_task(task)
    calls: list[Path] = []

    def fake_review(config, worktree, base, branch, output):  # noqa: ANN001
        calls.append(Path(worktree))
        return {"comments": []}

    monkeypatch.setattr("src.workflow.review", fake_review)
    lifecycle = _TaskLifecycle(engine, task.task_id, storage.root / "worktrees" / task.task_id)
    for repository in task.repository_names():
        worktree = storage.repository_worktree(task, repository)
        (worktree / "lifecycle-review.txt").write_text(repository)
        result = await lifecycle.run_tests("true playwright", repository=repository)
        assert result["passed"]

    await lifecycle.open_pr("publish both reviewed members")
    assert calls == [
        storage.repository_worktree(task, "shop/api"),
        storage.repository_worktree(task, "shop/web"),
    ]


@pytest.mark.asyncio
async def test_application_github_merge_event_accepts_case_changed_repository_name(
    tmp_path: Path,
) -> None:
    _, storage, engine, task = await _engine(tmp_path)
    task = _waiting_for_review(storage, task)
    await engine.handle_github_event(
        "pull_request",
        {
            "action": "closed",
            "repository": {"full_name": "SHOP/API"},
            "pull_request": {"number": 17, "merged": True},
        },
    )
    assert storage.load_task(task.task_id).repositories["shop/api"].merged is True


@pytest.mark.asyncio
async def test_application_review_event_accepts_case_changed_repository_name(
    tmp_path: Path,
) -> None:
    _, storage, engine, task = await _engine(tmp_path)
    task = _waiting_for_review(storage, task)
    await engine.handle_github_event(
        "pull_request_review_comment",
        {
            "repository": {"full_name": "SHOP/API"},
            "pull_request": {"number": 17},
            "comment": {
                "id": 52,
                "body": "Please handle this edge case",
                "user": {"login": "reviewer"},
                "author_association": "OWNER",
            },
        },
    )
    pending = storage.load_task(task.task_id).repositories["shop/api"].pending_review_comments
    assert [comment.id for comment in pending] == [52]


def test_legacy_single_segment_repository_name_still_creates_a_worktree(tmp_path: Path) -> None:
    source = _checkout(tmp_path / "backend", "app.py")
    config = Config(
        runtime_root=tmp_path / "runtime",
        repositories=[RepositoryConfig(name="backend", local_path=source, publish_mode="local")],
    )
    storage = Storage(config.runtime_root)
    task = storage.create_task(
        Incident(
            external_id="legacy-alias",
            source="test",
            repository="backend",
            environment="production",
            summary="legacy repository alias",
        ),
        config,
    )
    worktree = storage.create_worktree(task, local_path=source, repository="backend")
    assert worktree.is_dir()
    assert (worktree / ".git").exists()


def test_workspace_schema_migration_rolls_back_a_partial_upgrade(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE catalog_tasks (task_id TEXT PRIMARY KEY, scope TEXT NOT NULL, "
            "state TEXT NOT NULL, record TEXT NOT NULL, incident TEXT NOT NULL)"
        )
        db.execute("INSERT INTO catalog_tasks VALUES ('task-1', 'scope', 'received', '{}', '{}')")
        db.execute(
            "CREATE TABLE catalog_workspaces (task_id TEXT PRIMARY KEY, root TEXT NOT NULL, "
            "device INTEGER NOT NULL, inode INTEGER NOT NULL, active INTEGER NOT NULL)"
        )
        db.execute("INSERT INTO catalog_workspaces VALUES ('task-1', '/workspace/task-1', 1, 2, 1)")

    def crash_after_replacement(db):  # noqa: ANN001
        db.execute("ALTER TABLE catalog_workspaces RENAME TO catalog_workspaces_legacy")
        db.execute(
            "CREATE TABLE catalog_workspaces (task_id TEXT NOT NULL, repository TEXT NOT NULL, "
            "root TEXT NOT NULL, device INTEGER NOT NULL, inode INTEGER NOT NULL, "
            "active INTEGER NOT NULL, PRIMARY KEY(task_id, repository))"
        )
        raise RuntimeError("simulated process crash")

    monkeypatch.setattr(
        TaskCatalog, "_ensure_workspace_schema", staticmethod(crash_after_replacement)
    )
    with pytest.raises(RuntimeError, match="simulated process crash"):
        TaskCatalog(database)

    monkeypatch.undo()
    TaskCatalog(database)
    with sqlite3.connect(database) as db:
        row = db.execute(
            "SELECT root, device, inode, active FROM catalog_workspaces WHERE task_id = 'task-1'"
        ).fetchone()
    assert row == ("/workspace/task-1", 1, 2, 1)
