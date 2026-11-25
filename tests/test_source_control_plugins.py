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
from src.config import ApplicationConfig, Config, RepositoryConfig, load_config, save_config
from src.github import GitHubCLIAdapter, GitHubService
from src.models import Incident, PullRequestReference, ReviewComment, TaskState
from src.server import _source_control_health, create_server
from src.source_control import (
    create_source_control_provider,
    register_source_control_provider,
    register_webhook_router,
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


class _TestProvider:
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


@pytest.fixture
def mixed_provider_application(tmp_path):
    provider = _TestProvider()
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
    return application, provider, task, github_task


def test_application_build_dispatches_to_registered_provider(mixed_provider_application):
    application, provider, _, _ = mixed_provider_application
    assert application.source_control["test-provider"] is provider
    assert application.github.api is not None
    assert application.workflow.provider_for_repository("org/repo") is provider
    assert application.workflow.provider_for_repository("org/github-repo") is application.github
    assert isinstance(application.github, GitHubService)


@pytest.mark.asyncio
async def test_events_dispatch_to_registered_provider(mixed_provider_application):
    application, _, task, _ = mixed_provider_application
    payload = {"repository": {"full_name": "org/repo"}, "pull_request": {"number": 17}}

    assert (
        await application.workflow.handle_source_control_event("github", "issue_comment", payload)
        is None
    )
    result = await application.workflow.handle_source_control_event(
        "test-provider", "issue_comment", payload
    )
    assert result is not None and result.task_id == task.task_id


@pytest.mark.asyncio
async def test_provider_events_reject_repository_mismatches(mixed_provider_application):
    application, _, _, github_task = mixed_provider_application
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


@pytest.mark.asyncio
async def test_registered_provider_webhook_routes_clone_full_name_to_alias(tmp_path):
    provider = _TestProvider()
    register_source_control_provider("test-provider", lambda config, storage: provider)
    config = Config(
        runtime_root=tmp_path / ".agent",
        repositories=[RepositoryConfig(
            name="service",
            clone_url="https://git.example.com/company/service.git",
            publish_mode="test-provider",
        )],
    )
    storage = Storage(config.runtime_root)
    github = GitHubService(config.github)
    workflow = WorkflowEngine(
        config, storage, AsyncMock(), github, SimpleNamespace(accepts=lambda *_: True),
        source_control_providers={"github": github, "test-provider": provider},
    )
    task = storage.create_task(Incident(
        external_id="alias-issue", source="test", repository="service",
        environment="production", summary="test alias routing",
    ))
    task.pr_number = 17
    task.pr_head_sha = "head-sha"
    task.state = TaskState.WAITING_FOR_DEPLOYMENT
    storage.save_task(task)

    router = APIRouter()

    @router.post("/hooks/alias-provider")
    async def webhook(request: Request):
        result = await request.app.state.application.workflow.handle_source_control_event(
            "test-provider", "deployment_status", await request.json()
        )
        return {
            "task_id": result.task_id if result else None,
            "state": result.state.value if result else None,
        }

    provider.review_comment = lambda payload: ReviewComment(
        id=31, body="Please fix this", author="owner"
    )

    @router.post("/hooks/alias-review")
    async def review_webhook(request: Request):
        result = await request.app.state.application.workflow.handle_source_control_event(
            "test-provider", "issue_comment", await request.json()
        )
        return {"task_id": result.task_id if result else None}

    register_webhook_router(router)
    application = _application_stub(tmp_path)
    application.workflow = workflow
    client = TestClient(create_server(application, run_worker=False))
    response = client.post(
        "/hooks/alias-provider",
        json={
            "repository": {"full_name": "company/service"},
            "pull_request": {"number": 17},
            "deployment": {"sha": "head-sha", "environment": "preview"},
            "deployment_status": {"state": "success", "environment_url": "https://preview.test"},
        },
    )

    assert response.status_code == 200
    assert response.json() == {"task_id": task.task_id, "state": TaskState.TESTING_DEPLOYMENT.value}

    review_response = client.post(
        "/hooks/alias-review",
        json={
            "repository": {"full_name": "company/service"},
            "pull_request": {"number": 17},
        },
    )
    assert review_response.status_code == 200
    assert review_response.json() == {"task_id": task.task_id}
    pending = storage.load_task(task.task_id).pending_review_comments
    assert [comment.id for comment in pending] == [31]


@pytest.mark.asyncio
async def test_provider_event_rejects_alias_full_name_disagreement(tmp_path):
    provider = _TestProvider()
    register_source_control_provider("test-provider", lambda config, storage: provider)
    config = Config(
        runtime_root=tmp_path / ".agent",
        repositories=[RepositoryConfig(
            name="service",
            clone_url="https://git.example.com/company/service.git",
            publish_mode="test-provider",
        )],
    )
    storage = Storage(config.runtime_root)
    github = GitHubService(config.github)
    workflow = WorkflowEngine(
        config, storage, AsyncMock(), github, SimpleNamespace(accepts=lambda *_: True),
        source_control_providers={"github": github, "test-provider": provider},
    )
    task = storage.create_task(Incident(
        external_id="alias-disagreement", source="test", repository="service",
        environment="production", summary="test disagreement rejection",
    ))
    task.pr_number = 17
    storage.save_task(task)

    result = await workflow.handle_source_control_event(
        "test-provider", "deployment_status",
        {
            "repository": {"full_name": "company/service"},
            "deployment": {
                "repository": {"full_name": "another/service"},
                "sha": "head", "environment": "preview",
            },
        },
    )

    assert result is None


def test_publish_mode_is_normalized_and_validated():
    register_source_control_provider("my-provider", lambda *_: object())
    repository = RepositoryConfig(name="org/repo", publish_mode="  My-Provider  ")
    assert repository.publish_mode == "my-provider"
    with pytest.raises(ValidationError, match="provider name"):
        RepositoryConfig(name="org/repo", publish_mode="not a provider")


def test_config_rejects_unregistered_publish_mode(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[[repositories]]\nname = "org/repo"\npublish_mode = "missing-provider"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="not registered"):
        Config(repositories=[RepositoryConfig(name="org/repo", publish_mode="missing-provider")])
    with pytest.raises(ValidationError, match="not registered"):
        load_config(config_path, create=False)


def test_invalid_repository_container_remains_a_pydantic_error(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("repositories = 2\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="repositories"):
        load_config(config_path, create=False)


def test_application_build_wraps_provider_factory_failure_with_context(tmp_path):
    def fail_factory(config, storage):
        raise ValueError("factory failed")

    register_source_control_provider("broken-provider", fail_factory)
    config_path = tmp_path / "config.toml"
    save_config(
        Config(
            runtime_root=tmp_path / "runtime",
            repositories=[RepositoryConfig(name="org/repo", publish_mode="broken-provider")],
        ),
        config_path,
    )

    with pytest.raises(RuntimeError, match="broken-provider.*org/repo") as error:
        Application.build(config_path, agent_backend=AsyncMock())

    assert isinstance(error.value.__cause__, ValueError)


def test_foreign_provider_github_clone_keeps_git_access_adapter(tmp_path):
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

    assert isinstance(application.github.api, GitHubCLIAdapter)
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
    register_source_control_provider("plugin", lambda *_: object())
    plugin_repository = RepositoryConfig(
        name="org/plugin",
        clone_url="https://github.com/org/plugin.git",
        publish_mode="plugin",
    )
    assert plugin_repository.needs_github_adapter
    assert not plugin_repository.github_health_checkable
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
    register_webhook_router(router)
    assert source_control._webhook_routers.count(router) == 1
    app = create_server(_application_stub(tmp_path), run_worker=False)

    response = TestClient(app).post("/hooks/test-provider", json={})

    assert response.status_code == 200
    assert response.json() == {"accepted": True, "task_id": "translated"}


def test_native_github_route_uses_provider_webhook_contract(tmp_path):
    provider = SimpleNamespace(
        authenticate_and_translate=lambda headers, body: (
            "pull_request",
            {"number": 17},
            False,
        )
    )
    application = _application_stub(tmp_path)
    application.source_control = {"github": provider}
    application.workflow.handle_source_control_event = AsyncMock(
        return_value=SimpleNamespace(task_id="translated")
    )
    app = create_server(application, run_worker=False)

    response = TestClient(app).post("/hooks/github", content=b"raw-body")

    assert response.status_code == 202
    assert response.json() == {"accepted": True, "task_id": "translated"}
    application.workflow.handle_source_control_event.assert_awaited_once_with(
        "github", "pull_request", {"number": 17}
    )


@pytest.mark.asyncio
async def test_health_checks_registered_source_control_providers():
    provider = SimpleNamespace(
        check_health=AsyncMock(return_value={"example:org/repo": {"status": "ok"}})
    )
    application = SimpleNamespace(source_control={"example": provider})

    assert await _source_control_health(application) == {
        "example:org/repo": {"status": "ok"}
    }
    provider.check_health.assert_awaited_once_with()


def test_server_openapi_version_uses_default_version(tmp_path):
    app = create_server(_application_stub(tmp_path), run_worker=False)

    assert app.version == "0.1.0"


def test_provider_registration_rejects_invalid_and_duplicate_names():
    register_source_control_provider("duplicate-provider", lambda config, storage: object())

    with pytest.raises(ValueError, match="cannot be 'auto'"):
        register_source_control_provider("auto", lambda config, storage: object())
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
    register_source_control_provider("test-provider", lambda *_: object())
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
    register_source_control_provider("plugin", lambda *_: object())
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
