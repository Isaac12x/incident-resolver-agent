"""Public extension points for source control integrations."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient

from src import source_control
from src.app import Application
from src.config import Config, RepositoryConfig, save_config
from src.github import GitHubCLIAdapter, GitHubService
from src.models import Incident
from src.server import create_server
from src.source_control import (
    create_source_control_provider,
    register_source_control_provider,
    register_webhook_router,
    registered_source_control_provider_names,
)


@pytest.fixture(autouse=True)
def isolated_plugin_registry(monkeypatch):
    monkeypatch.setattr(
        source_control,
        "_provider_factories",
        source_control._provider_factories.copy(),
    )
    monkeypatch.setattr(source_control, "_webhook_routers", [])


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
            repository = payload.get("repository", {}).get("full_name")
            number = payload.get("pull_request", {}).get("number")
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
            "- `org/github`: [PR #7](https://github.test/7) (SHA `sha-7`)"
        },
    )


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
