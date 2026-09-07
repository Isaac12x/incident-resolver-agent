"""Real local Git publication with GitHub API calls captured at the transport boundary."""

import json
import subprocess
from unittest.mock import patch

import pytest

from src.app import Application
from src.config import Config, RepositoryConfig, save_config
from src.github import GitHubCLIAdapter
from src.models import Incident, TaskEvent, TaskState, VerificationResult
from src.storage import Storage
from src.systemd_env import build_systemd_environment
from src.tooling import install_configured_repositories, repository_candidates
from src.workflow import _TaskLifecycle


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def publication(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "--initial-branch=main")
    source_root = tmp_path / "sources"
    source = source_root / "Company--Application"
    source.mkdir(parents=True)
    git(source, "init", "--initial-branch=main")
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.test")
    (source / "app.txt").write_text("broken\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "base")
    git(source, "remote", "add", "origin", str(remote))
    git(source, "push", "origin", "main")
    config = Config(
        runtime_root=tmp_path / ".agent",
        repositories=[
            RepositoryConfig(
                name="company/application",
                clone_url=str(remote),
                publish_mode="github",
            )
        ],
    )
    config_path = tmp_path / "config.toml"
    save_config(config, config_path)
    installed = install_configured_repositories(
        config_path, source_root, config.runtime_root / "repositories"
    )
    assert len(installed) == 1
    storage = Storage(config.runtime_root)
    task = storage.create_task(
        Incident(
            external_id="INC-123",
            source="test",
            repository="company/application",
            environment="production",
            summary="Handle missing invoice",
        )
    )
    worktree = storage.create_worktree(task, str(remote))
    assert task.branch.startswith("incident-harness/fix/handle-missing-invoice-")
    (worktree / "app.txt").write_text("fixed\n")
    storage.write_artifact(task.task_id, "investigation.md", "Missing invoice was dereferenced.")
    storage.write_artifact(
        task.task_id, "artifacts/local/fix.txt", "Handle missing invoice; regression passed."
    )
    storage.append_event(
        task.task_id,
        TaskEvent(
            type="verification.local",
            data={"command": "pytest tests/test_invoice.py", "passed": True},
        ),
    )
    adapter = GitHubCLIAdapter(config, storage)
    return config, storage, task, worktree, remote, adapter


@pytest.mark.asyncio
async def test_publishes_verified_commit_and_reuses_pr_on_retry(publication):
    config, storage, task, worktree, remote, adapter = publication
    calls = []
    pulls = []
    original = subprocess.run

    def transport(command, **kwargs):
        if command[0] != "gh":
            return original(command, **kwargs)
        endpoint = command[4]
        data = json.loads(kwargs["input"]) if kwargs["input"] else None
        calls.append((endpoint, data))
        sha = git(remote, "rev-parse", f"refs/heads/{task.branch}")
        if data:
            assert data["draft"] is True and data["base"] == "main"
            assert data["head"] == task.branch
            assert "Missing invoice was dereferenced" in data["body"]
            assert "pytest tests/test_invoice.py" in data["body"]
            assert "Pending verification" in data["body"]
            pulls.append(
                {
                    "number": 7,
                    "html_url": "https://github.com/company/application/pull/7",
                    "head": {"sha": sha},
                }
            )
            response = pulls[0]
        else:
            response = pulls
        return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

    with patch("src.github.subprocess.run", side_effect=transport):
        reference = await adapter("create_pull_request", task.model_dump(mode="json"))
        second = await adapter("create_pull_request", task.model_dump(mode="json"))
    assert reference == second
    assert len([data for _, data in calls if data]) == 1
    assert git(remote, "show", f"{task.branch}:app.txt") == "fixed"
    assert git(remote, "show", "main:app.txt") == "broken"
    assert reference["head_sha"] == git(worktree, "rev-parse", "HEAD")
    assert json.loads((storage.task_directory(task.task_id) / "pr.json").read_text()) == reference
    # Fetching the original mirror must preserve unpublished incident branches.
    Storage._refresh_repository(
        config.runtime_root / "repositories/company--application.git", "main"
    )
    assert git(worktree, "branch", "--show-current") == task.branch


@pytest.mark.asyncio
async def test_update_commits_and_tracks_exact_head(publication):
    config, storage, task, worktree, remote, adapter = publication
    from types import SimpleNamespace

    from src.github import GitHubService

    task = storage.transition(task.task_id, TaskState.TESTING_LOCAL, pr_number=7)

    def api(endpoint, *args):
        return {
            "number": 7,
            "html_url": "https://github.com/company/application/pull/7",
            "head": {"sha": git(remote, "rev-parse", task.branch)},
        }

    workflow = SimpleNamespace(
        config=config, storage=storage, github=GitHubService(config.github, api=adapter)
    )
    with patch.object(adapter, "_api", side_effect=api):
        result = await _TaskLifecycle(workflow, task.task_id, worktree).open_pr("Verified fix")
    assert result["state"] == TaskState.WAITING_FOR_DEPLOYMENT
    assert storage.load_task(task.task_id).pr_head_sha == git(worktree, "rev-parse", "HEAD")


@pytest.mark.asyncio
async def test_git_failure_prevents_api_and_pr_success(publication):
    _, _, task, worktree, _, adapter = publication
    git(worktree, "remote", "set-url", "origin", "/does-not-exist")
    with patch.object(adapter, "_api") as api, pytest.raises(RuntimeError, match="failed"):
        await adapter("create_pull_request", task.model_dump(mode="json"))
    api.assert_not_called()
    with pytest.raises(RuntimeError, match="without an incident branch"):
        await adapter(
            "create_pull_request", task.model_copy(update={"branch": None}).model_dump(mode="json")
        )


@pytest.mark.asyncio
async def test_missing_auth_fails_instead_of_local_publication(publication):
    config, _, task, _, _, adapter = publication
    with (
        patch(
            "src.github.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1, "", "not logged in"),
        ),
        pytest.raises(RuntimeError, match="not logged in"),
    ):
        adapter._api("repos/company/application/pulls")
    path = config.runtime_root.parent / "app.toml"
    save_config(config, path)
    app = Application.build(path, agent_backend=object())
    assert isinstance(app.github.api, GitHubCLIAdapter)
    with (
        patch.object(adapter, "_api", return_value={"head": {"sha": "stale"}}),
        pytest.raises(RuntimeError, match="confirmed the pushed PR head"),
    ):
        await adapter(
            "update_pull_request",
            task.model_copy(update={"pr_number": 7}).model_dump(mode="json"),
        )


def test_verification_status_and_unsupported_operation(publication):
    _, _, task, _, _, adapter = publication
    result = VerificationResult(
        passed=True, environment="preview", sha="verified-sha", url="https://preview.test"
    )
    with patch(
        "src.github.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")
    ) as run:
        adapter._operate(
            "publish_verification", {"task": task.model_dump(), "result": result.model_dump()}
        )
        data = json.loads(run.call_args.kwargs["input"])
        assert data["state"] == "success"
        assert run.call_args.args[0][4].endswith("/statuses/verified-sha")
    with pytest.raises(ValueError, match="unsupported"):
        adapter._operate("invalid", {})


def test_credentials_export_and_case_insensitive_discovery(tmp_path):
    config = Config()
    values = {
        "GH_TOKEN": "test-only-token",
        "GITHUB_TOKEN": "test-only-fallback",
        "GH_CONFIG_DIR": "/var/lib/incident-harness/.config/gh",
        "UNRELATED": "excluded",
    }
    assert build_systemd_environment(config, values) == {
        k: v for k, v in values.items() if k != "UNRELATED"
    }
    storage = Storage(tmp_path / ".agent")
    source = storage.root / "repositories/Company--Application"
    source.mkdir()
    git(source, "init")
    assert storage.local_repository("company/application") == source
    nested = tmp_path / "nested/Company/Application"
    nested.mkdir(parents=True)
    assert nested in repository_candidates(tmp_path / "nested", "company/application")
    (source.parent / "company--application").mkdir()
    with pytest.raises(ValueError, match="ambiguous"):
        repository_candidates(source.parent, "company/application")


def test_selected_repository_and_github_events_ignore_owner_case(publication):
    config, storage, task, _, _, _ = publication
    assert config.repository("Company/Application") is config.repositories[0]
    task = storage.transition(task.task_id, TaskState.WAITING_FOR_DEPLOYMENT, pr_number=7)
    assert storage.find_by_pr("Company/Application", 7).task_id == task.task_id


@pytest.mark.asyncio
async def test_live_repository_lock_does_not_exhaust_task_budget(publication):
    from types import SimpleNamespace

    from src.github import GitHubService
    from src.verify import DeploymentVerifier
    from src.workflow import WorkflowEngine

    config, storage, original, _, remote, _ = publication
    incident = storage.load_incident(original.task_id).model_copy(update={"external_id": "second"})
    task = storage.create_task(incident)
    config.model.max_task_iterations = 1
    workflow = WorkflowEngine(
        config, storage, SimpleNamespace(), GitHubService(config.github), DeploymentVerifier(config)
    )
    with storage.lock(task.repository):
        waiting = await workflow.process(task.task_id)
        assert waiting.state == TaskState.RECEIVED
        assert waiting.attempts == 0 and waiting.error is None
        assert not any(e.type == "task.error" for e in storage.events(task.task_id))
    ready = await workflow.process(task.task_id)
    assert ready.state == TaskState.COLLECTING_CONTEXT
    assert ready.branch.startswith("incident-harness/fix/")
