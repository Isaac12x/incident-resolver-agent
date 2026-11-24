"""Public extension points for source control integrations."""

import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src import source_control
from src.app import Application
from src.config import ApplicationConfig, Config, RepositoryConfig, save_config
from src.github import GitHubCLIAdapter, GitHubService
from src.models import Incident, PullRequestReference, TaskState
from src.server import create_server
from src.source_control import (
    create_source_control_provider,
    register_source_control_provider,
    register_webhook_router,
    registered_source_control_provider_names,
)
from src.storage import Storage
from src.workflow import WorkflowEngine


@pytest.fixture(autouse=True)
def isolated_plugin_registry(monkeypatch):
    monkeypatch.setattr(
        source_control,
        "_provider_factories",
        source_control._provider_factories.copy(),
    )
    monkeypatch.setattr(source_control, "_webhook_routers", source_control._webhook_routers.copy())


@pytest.mark.asyncio
async def test_application_build_dispatches_to_registered_provider(tmp_path):
    class Provider:
        api = object()
        publish_enabled = True

        async def create_pull_request(self, task):
            raise NotImplementedError

        async def update_pull_request(self, task):
            raise NotImplementedError

        async def publish_verification(self, task, result):
            raise NotImplementedError

        def repository_and_pr(self, payload):
            pull_request = payload.get("pull_request", {})
            repository = pull_request.get("repository") or payload.get("repository", {}).get(
                "full_name"
            )
            number = pull_request.get("number")
            return (repository, number) if repository and number else None

        def review_comment(self, payload):
            return None

    provider = Provider()
    register_source_control_provider("test-provider", lambda config, storage: provider)
    config_path = tmp_path / ".agent" / "config.toml"
    provider_config = Config(
        runtime_root=tmp_path / ".agent",
        repositories=[
            RepositoryConfig(name="org/repo", publish_mode="test-provider"),
            RepositoryConfig(
                name="org/github-repo",
                clone_url="https://github.com/org/github-repo.git",
                publish_mode="auto",
            ),
        ],
    )
    save_config(
        provider_config,
        config_path,
    )

    application = Application.build(config_path, agent_backend=AsyncMock())

    assert application.source_control["test-provider"] is provider
    assert application.github.api is not None
    assert application.workflow.provider_for_repository("org/repo") is provider
    assert application.workflow.provider_for_repository("org/github-repo") is application.github
    assert isinstance(application.github, GitHubService)
    assert "test-provider" in registered_source_control_provider_names()
    task = application.storage.create_task(
        Incident(
            external_id="issue-1",
            source="test",
            repository="org/repo",
            environment="production",
            summary="test incident",
        )
    )
    task.pr_number = 17
    application.storage.save_task(task)
    payload = {"repository": {"full_name": "org/repo"}, "pull_request": {"number": 17}}

    assert await application.workflow.handle_github_event("issue_comment", payload) is None
    result = await application.workflow.handle_source_control_event(
        "test-provider", "issue_comment", payload
    )
    assert result is not None and result.task_id == task.task_id
    mismatched_deployment = {
        "repository": {"full_name": "org/github-repo"},
        "pull_request": {"repository": "org/repo", "number": 17},
        "deployment": {
            "sha": "deployment-sha",
            "environment": "preview",
            "repository": {"full_name": "org/github-repo"},
        },
    }
    assert (
        await application.workflow.handle_source_control_event(
            "test-provider", "deployment_status", mismatched_deployment
        )
        is None
    )
    mismatched_nested_deployment = {
        "repository": {"full_name": "org/repo"},
        "pull_request": {"repository": "org/repo", "number": 17},
        "deployment": {
            "sha": "deployment-sha",
            "environment": "preview",
            "repository": {"full_name": "org/github-repo"},
        },
    }
    assert (
        await application.workflow.handle_source_control_event(
            "test-provider", "deployment_status", mismatched_nested_deployment
        )
        is None
    )
    github_task = application.storage.create_task(
        Incident(
            external_id="issue-github",
            source="test",
            repository="org/github-repo",
            environment="production",
            summary="GitHub incident",
        )
    )
    github_task.pr_number = 23
    application.storage.save_task(github_task)
    foreign_provider_event = {
        "repository": {"full_name": "org/github-repo"},
        "pull_request": {"repository": "org/github-repo", "number": 23},
        "deployment": {"sha": "github-sha", "environment": "preview"},
    }
    assert (
        await application.workflow.handle_source_control_event(
            "test-provider", "deployment_status", foreign_provider_event
        )
        is None
    )


def test_publish_mode_is_normalized_and_validated():
    repository = RepositoryConfig(name="org/repo", publish_mode="  My-Provider  ")
    assert repository.publish_mode == "my-provider"
    with pytest.raises(ValidationError, match="provider name"):
        RepositoryConfig(name="org/repo", publish_mode="not a provider")


def test_foreign_provider_github_clone_does_not_enable_github_api(tmp_path):
    register_source_control_provider("plugin", lambda config, storage: SimpleNamespace())
    config_path = tmp_path / ".agent" / "config.toml"
    save_config(
        Config(
            runtime_root=tmp_path / ".agent",
            repositories=[
                RepositoryConfig(
                    name="org/repo",
                    clone_url="https://github.com/org/repo.git",
                    publish_mode="plugin",
                )
            ],
        ),
        config_path,
    )

    application = Application.build(config_path, agent_backend=AsyncMock())

    assert application.github.api is None
    save_config(
        Config(
            runtime_root=tmp_path / ".agent",
            repositories=[
                RepositoryConfig(
                    name="org/repo",
                    clone_url="https://github.com/org/repo.git",
                    publish_mode="local",
                )
            ],
        ),
        config_path,
    )
    legacy_application = Application.build(config_path, agent_backend=AsyncMock())

    assert isinstance(legacy_application.github.api, GitHubCLIAdapter)


@pytest.mark.asyncio
async def test_github_health_skips_plugin_owned_github_clone_urls():
    config = Config(
        repositories=[
            RepositoryConfig(
                name="org/plugin",
                clone_url="https://github.com/org/plugin.git",
                publish_mode="plugin",
            ),
            RepositoryConfig(
                name="org/github",
                clone_url="https://github.com/org/github.git",
                publish_mode="auto",
            ),
        ]
    )
    calls = []

    async def health_command(*command):
        calls.append(command)
        return '{"permissions":{"push":true}}' if command[0] == "gh" else ""

    adapter = GitHubCLIAdapter(config, object())
    with patch.object(adapter, "_health_command", side_effect=health_command):
        result = await adapter.check_health()

    assert set(result) == {"github:org/github"}
    assert all("org/plugin" not in command for command in calls)


@pytest.mark.asyncio
async def test_workflow_publishes_create_and_update_through_each_repository_provider(tmp_path):
    class PluginProvider:
        publish_enabled = True

        def __init__(self):
            self.calls = []

        async def create_pull_request(self, task):
            self.calls.append(("create", task.repository))
            return PullRequestReference(
                repository=task.repository,
                number=71,
                url="https://example.test/71",
                head_sha="plugin-create-sha",
                branch=task.branch or "plugin-branch",
            )

        async def update_pull_request(self, task):
            self.calls.append(("update", task.repository))
            return PullRequestReference(
                repository=task.repository,
                number=task.pr_number or 0,
                url=task.pr_url or "https://example.test/71",
                head_sha="plugin-update-sha",
                branch=task.branch or "plugin-branch",
            )

        async def publish_verification(self, task, result):
            return None

        def repository_and_pr(self, payload):
            return None

        def review_comment(self, payload):
            return None

    plugin = PluginProvider()
    register_source_control_provider("plugin", lambda config, storage: plugin)
    config_path = tmp_path / ".agent" / "config.toml"
    save_config(
        Config(
            runtime_root=tmp_path / ".agent",
            repositories=[
                RepositoryConfig(name="org/plugin", publish_mode="plugin"),
                RepositoryConfig(
                    name="org/github",
                    clone_url="https://github.com/org/github.git",
                    publish_mode="auto",
                ),
            ],
        ),
        config_path,
    )
    application = Application.build(config_path, agent_backend=AsyncMock())
    application.github.api = object()
    github_create = AsyncMock()
    github_update = AsyncMock()
    application.github.create_pull_request = github_create
    application.github.update_pull_request = github_update

    plugin_worktree = _git_worktree(tmp_path / "plugin-worktree")
    plugin_task = _publication_task(application, "org/plugin")
    plugin_created = PullRequestReference(
        repository="org/plugin",
        number=19,
        url="https://github.test/19",
        head_sha="github-create-sha",
        branch="github-branch",
    )
    plugin_updated = plugin_created.model_copy(update={"head_sha": "github-update-sha"})
    github_create.return_value = plugin_created
    github_update.return_value = plugin_updated

    created = await application.workflow._publish(plugin_task, plugin_worktree)
    plugin_task.pr_number = created.number
    plugin_task.pr_url = created.url
    plugin_task.pr_head_sha = created.head_sha
    plugin_task.branch = created.branch
    _change_worktree(plugin_worktree)
    updated = await application.workflow._publish(plugin_task, plugin_worktree, update=True)

    assert created.number == 71 and updated.head_sha == "plugin-update-sha"
    assert plugin.calls == [("create", "org/plugin"), ("update", "org/plugin")]

    github_worktree = _git_worktree(tmp_path / "github-worktree")
    github_task = _publication_task(application, "org/github")
    github_create.return_value = plugin_created.model_copy(
        update={"repository": "org/github", "number": 29}
    )
    github_update.return_value = plugin_updated.model_copy(
        update={"repository": "org/github", "number": 29}
    )
    github_created = await application.workflow._publish(github_task, github_worktree)
    github_task.pr_number = github_created.number
    github_task.pr_url = github_created.url
    github_task.pr_head_sha = github_created.head_sha
    github_task.branch = github_created.branch
    _change_worktree(github_worktree)
    github_updated = await application.workflow._publish(
        github_task, github_worktree, update=True
    )

    assert github_updated.repository == "org/github"
    github_create.assert_awaited_once()
    github_update.assert_awaited_once()


def _publication_task(application, repository):
    return application.storage.create_task(
        Incident(
            external_id=f"publish-{repository}",
            source="test",
            repository=repository,
            environment="production",
            summary="publication test",
        )
    )


def _git_worktree(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.test"], check=True
    )
    (path / "file.txt").write_text("first\n")
    subprocess.run(["git", "-C", str(path), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "first"], check=True)
    return path


def _change_worktree(path):
    (path / "file.txt").write_text("second\n")
    subprocess.run(["git", "-C", str(path), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "second"], check=True)


def test_registered_webhook_router_is_mounted(tmp_path):
    router = APIRouter()

    @router.post("/hooks/test-provider")
    async def webhook(request: Request):
        application = request.app.state.application
        task = await application.workflow.handle_source_control_event(
            "test-provider", "pull_request", await request.json()
        )
        return {"accepted": application is not None, "task_id": task}

    register_webhook_router(router)
    app = create_server(_application_stub(tmp_path), run_worker=False)

    response = TestClient(app).post("/hooks/test-provider", json={})

    assert response.status_code == 200
    assert response.json() == {"accepted": True, "task_id": "translated"}


def test_provider_registration_rejects_invalid_and_duplicate_names():
    register_source_control_provider("duplicate-provider", lambda config, storage: object())

    with pytest.raises(ValueError, match="cannot be 'auto'"):
        register_source_control_provider("auto", lambda config, storage: object())
    with pytest.raises(ValueError, match="provider name"):
        register_source_control_provider("not a provider", lambda config, storage: object())
    with pytest.raises(ValueError, match="provider name"):
        register_source_control_provider("not a provider", lambda config, storage: object())
    with pytest.raises(ValueError, match="already registered"):
        register_source_control_provider("duplicate-provider", lambda config, storage: object())
    with pytest.raises(ValueError, match="not registered"):
        create_source_control_provider("missing-provider", Config(), StorageStub())


def test_webhook_registration_requires_fastapi_router():
    with pytest.raises(TypeError, match="APIRouter"):
        register_webhook_router(object())


def test_grouped_github_sync_only_updates_github_members(tmp_path):
    config = Config(
        repositories=[
            RepositoryConfig(name="org/github", publish_mode="github"),
            RepositoryConfig(name="org/plugin", publish_mode="test-provider"),
        ]
    )
    task = SimpleNamespace(
        repositories={
            "org/github": SimpleNamespace(
                pr_number=7, pr_url="https://github.test/7", pr_head_sha="sha-7"
            ),
            "org/plugin": SimpleNamespace(
                pr_number=8, pr_url="https://example.test/8", pr_head_sha="sha-8"
            ),
        }
    )
    adapter = GitHubCLIAdapter(config, object())

    with (
        patch("src.application_workflow._view", return_value=object()),
        patch.object(adapter, "_body", return_value="PR body"),
        patch.object(adapter, "_api") as api,
    ):
        adapter._sync_application(task)

    api.assert_called_once_with(
        "repos/org/github/pulls/7",
        "PATCH",
        {
            "body": "PR body\n\n## Application pull requests\n\n"
            "- `org/github`: [PR #7](https://github.test/7) (SHA `sha-7`)\n"
            "- `org/plugin`: [PR #8](https://example.test/8) (SHA `sha-8`)"
        },
    )


@pytest.mark.asyncio
async def test_plugin_deployment_without_repo_field_uses_target_member_in_group(tmp_path):
    config = Config(
        runtime_root=tmp_path / "runtime",
        repositories=[
            RepositoryConfig(name="org/api", publish_mode="github"),
            RepositoryConfig(name="org/web", publish_mode="plugin"),
        ],
        applications=[
            ApplicationConfig(name="shop", repositories=["org/api", "org/web"])
        ],
    )
    storage = Storage(config.runtime_root)
    task = storage.create_task(
        Incident(
            external_id="group-deployment",
            source="test",
            repository="org/api",
            application="shop",
            environment="production",
            summary="group deployment",
        ),
        config,
    )
    task.state = TaskState.WAITING_FOR_DEPLOYMENT
    task.repositories["org/web"].pr_number = 12
    task.repositories["org/web"].pr_head_sha = "web-head"
    storage.save_task(task)

    class Plugin:
        publish_enabled = True

        @staticmethod
        def repository_and_pr(payload):
            return "org/web", 12

        @staticmethod
        def review_comment(payload):
            return None

    github = GitHubService(config.github)
    engine = WorkflowEngine(
        config,
        storage,
        None,
        github,
        SimpleNamespace(accepts=lambda *_: True),
        source_control_providers={"github": github, "plugin": Plugin()},
    )

    result = await engine.handle_source_control_event(
        "plugin",
        "deployment_status",
        {"deployment": {"sha": "web-head", "environment": "preview"}},
    )

    assert result is not None
    assert result.repositories["org/web"].deployment_sha == "web-head"
    assert result.repositories["org/api"].deployment_sha is None


def _application_stub(tmp_path):
    from types import SimpleNamespace

    return SimpleNamespace(
        connectors=SimpleNamespace(start=_noop, stop=_noop, configs={}, health=_empty),
        workflow=SimpleNamespace(
            stop=lambda: None,
            run_worker=_noop,
            handle_source_control_event=_translated,
        ),
        config=SimpleNamespace(
            server=SimpleNamespace(api_token_env="TOKEN", require_api_auth=False),
            trigger=SimpleNamespace(hook_path="/hooks/incidents"),
        ),
        storage=SimpleNamespace(telemetry=SimpleNamespace(record=lambda *_, **__: None)),
    )


class StorageStub:
    pass


async def _noop():
    return None


async def _empty():
    return {}


async def _translated(provider, event, payload):
    return "translated"
