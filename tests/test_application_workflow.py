"""Focused aggregate lifecycle checks for application scoped tasks."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.application_workflow import (
    ApplicationWorkflow,
    _state,
    _update_state,
    _value,
    _view,
)
from src.config import ApplicationConfig, Config, RepositoryConfig
from src.github import GitHubCLIAdapter
from src.models import DeploymentReference, Incident, RepositoryTaskState, TaskRecord
from src.storage import Storage
from src.verify import DeploymentVerifier


def task_with_scope() -> TaskRecord:
    return TaskRecord(
        external_id="APP-1",
        source="test",
        conversation_id="incident:app-1",
        repository="api",
        environment="production",
        summary="broken",
        application="shop",
        repositories={
            "api": RepositoryTaskState(repository="api", branch="fix/api"),
            "web": RepositoryTaskState(repository="web", branch="fix/web"),
        },
    )


def test_member_projection_and_scope_rejection() -> None:
    task = task_with_scope()
    assert _state(task, "API").branch == "fix/api"
    assert _value(task, "web", "branch") == "fix/web"
    assert _view(task, "web").repository == "web"
    _update_state(task, "api", changed=True)
    assert task.repositories["api"].changed
    with pytest.raises(KeyError):
        _view(task, "other")
    with pytest.raises(KeyError):
        _update_state(task, "other", changed=True)


def test_legacy_projection_keeps_primary_fields() -> None:
    task = TaskRecord(
        external_id="LEGACY-1",
        source="test",
        conversation_id="incident:legacy-1",
        repository="api",
        environment="production",
        summary="broken",
        branch="fix/legacy",
    )
    assert _value(task, "api", "branch") == "fix/legacy"
    _update_state(task, "api", pr_number=12)
    assert task.pr_number == 12


def test_integration_revision_fails_closed_for_missing_git(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    storage = Storage(root)
    task = storage.create_task(
        Incident(
            external_id="APP-2",
            source="test",
            repository="api",
            application="shop",
            environment="production",
            summary="broken",
        ),
        Config(
            runtime_root=root,
            repositories=[RepositoryConfig(name="api", local_path=tmp_path / "api")],
            applications=[ApplicationConfig(name="shop", repositories=["api"])],
        ),
    )
    (tmp_path / "api").mkdir()
    engine = SimpleNamespace(
        storage=storage,
        config=Config(
            runtime_root=root,
            repositories=[RepositoryConfig(name="api", local_path=tmp_path / "api")],
            applications=[ApplicationConfig(name="shop", repositories=["api"])],
        ),
    )
    with pytest.raises(RuntimeError, match="integration inputs"):
        ApplicationWorkflow(engine).integration_revision(task)


def test_replaced_member_workspace_is_rejected(tmp_path: Path) -> None:
    repository = tmp_path / "api"
    repository.mkdir()
    subprocess.run(["git", "init", "-qb", "main", str(repository)], check=True)
    (repository / "value.txt").write_text("base")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    config = Config(
        runtime_root=tmp_path / "runtime",
        repositories=[RepositoryConfig(name="api", local_path=repository)],
        applications=[ApplicationConfig(name="shop", repositories=["api"])],
    )
    storage = Storage(config.runtime_root)
    task = storage.create_task(
        Incident(
            external_id="APP-3",
            source="test",
            repository="api",
            application="shop",
            environment="production",
            summary="broken",
        ),
        config,
    )
    worktree = storage.create_application_worktrees(task, config)["api"]
    engine = SimpleNamespace(storage=storage, config=config)
    workflow = ApplicationWorkflow(engine)
    worktree.rename(worktree.with_name("replaced"))
    with pytest.raises(FileNotFoundError):
        workflow.verify_workspaces(storage.load_task(task.task_id))


def test_github_application_sync_links_all_sibling_prs(tmp_path: Path) -> None:
    config = Config(
        runtime_root=tmp_path / "runtime",
        repositories=[RepositoryConfig(name="api"), RepositoryConfig(name="web")],
        applications=[ApplicationConfig(name="shop", repositories=["api", "web"])],
    )
    storage = Storage(config.runtime_root)
    task = storage.create_task(
        Incident(
            external_id="APP-4",
            source="test",
            repository="api",
            application="shop",
            environment="production",
            summary="broken",
        ),
        config,
    )
    task.repositories["api"].pr_number = 11
    task.repositories["api"].pr_url = "https://github.test/shop/api/pull/11"
    task.repositories["api"].pr_head_sha = "api-sha"
    task.repositories["web"].pr_number = 12
    task.repositories["web"].pr_url = "https://github.test/shop/web/pull/12"
    task.repositories["web"].pr_head_sha = "web-sha"
    storage.save_task(task)
    calls: list[tuple[str, dict]] = []
    adapter = GitHubCLIAdapter(config, storage)
    adapter._api = lambda endpoint, method="GET", data=None: calls.append((endpoint, data)) or {}
    adapter._sync_application(task)
    assert len(calls) == 2
    assert all(call[0].endswith(("/pulls/11", "/pulls/12")) for call in calls)
    assert all("https://github.test/shop/api/pull/11" in call[1]["body"] for call in calls)
    assert all("https://github.test/shop/web/pull/12" in call[1]["body"] for call in calls)


@pytest.mark.asyncio
async def test_application_verifier_routes_member_and_records_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config(
        runtime_root=tmp_path / "runtime",
        repositories=[RepositoryConfig(name="api"), RepositoryConfig(name="web")],
        applications=[ApplicationConfig(name="shop", repositories=["api", "web"])],
    )
    storage = Storage(config.runtime_root)
    task = storage.create_task(
        Incident(
            external_id="APP-5",
            source="test",
            repository="api",
            application="shop",
            environment="production",
            summary="broken",
        ),
        config,
    )
    task.repositories["web"].pr_number = 12
    task.repositories["web"].pr_head_sha = "web-sha"
    deployment = DeploymentReference(
        repository="web", environment="preview", sha="web-sha", url="https://preview.test"
    )
    config.repository("web").playwright.command = "python -c pass"
    transport = httpx.MockTransport(lambda _: httpx.Response(200))
    verifier = DeploymentVerifier(config, client=httpx.AsyncClient(transport=transport))
    assert verifier.accepts(task, deployment, "web")

    class TimedOutProcess:
        returncode = None

        async def communicate(self):
            raise TimeoutError

        def kill(self):
            return None

        async def wait(self):
            return None

    async def spawn(*_args, **_kwargs):
        return TimedOutProcess()

    monkeypatch.setattr("src.verify.asyncio.create_subprocess_exec", spawn)
    config.repository("web").playwright.timeout_seconds = 0.01
    result = await verifier.verify(task, deployment, tmp_path, "web")
    assert not result.passed and "timed out" in (result.reason or "")
    await verifier.client.aclose()
