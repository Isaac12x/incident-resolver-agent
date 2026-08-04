from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from agents import ModelSettings
from fastapi.testclient import TestClient
from textual.widgets import Button, Input, Select, Static, TabbedContent, TextArea

from src.__main__ import _run_direct, _worker, main, parse_arguments
from src.agent import IncidentAgent, OpenAIAgentsBackend
from src.app import Application
from src.config import (
    AgentConfig,
    Config,
    ConnectorConfig,
    ModelConfig,
    PermissionsConfig,
    RepositoryConfig,
    SafetyConfig,
    TriggerConfig,
    load_config,
    save_config,
)
from src.connectors import ConnectorManager, ConnectorTestResult
from src.github import GitHubService, WebhookSignatureError
from src.models import (
    DeploymentReference,
    FixResult,
    Incident,
    InvestigationResult,
    PullRequestReference,
    ReviewComment,
    ReviewResult,
    TaskEvent,
    TaskState,
    VerificationResult,
)
from src.server import create_server
from src.storage import Storage
from src.tooling import (
    ToolResult,
    build_repository_graphs,
    capture_structured_tree,
    clone_and_index_repository,
    github_login,
    list_github_repositories,
    repository_name_from_url,
    repository_slug,
)
from src.tools import CommandResult, ToolError, WorkspaceTools
from src.tui import ConfigurationApp, run_tui
from src.verify import DeploymentVerifier
from src.workflow import WorkflowEngine


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        runtime_root=tmp_path / ".agent",
        poll_interval_seconds=0.01,
        repositories=[
            RepositoryConfig(
                name="company/application",
                clone_url="https://example.invalid/company/application.git",
                incident_environments=["production", "staging"],
                verification_environment="preview",
            )
        ],
        connectors=[ConnectorConfig(name="sentry", type="webhook")],
    )


@pytest.fixture
def incident() -> Incident:
    return Incident(
        external_id="INC-1842",
        source="sentry",
        repository="company/application",
        environment="production",
        summary="Checkout returns 500",
        description="Anonymous session is missing",
    )


def test_configuration_round_trip_and_validation(config: Config, tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "config.toml"
    save_config(config, path)
    loaded = load_config(path)
    assert loaded == config
    assert "GITHUB_WEBHOOK_SECRET" in path.read_text()
    assert loaded.repository("company/application").verification_environment == "preview"
    with pytest.raises(KeyError):
        loaded.repository("missing/repository")
    with pytest.raises(ValueError, match="require url"):
        ConnectorConfig(name="bad")
    created = load_config(tmp_path / "new" / "config.toml")
    assert created.runtime_root == tmp_path / "new"
    assert created.agent.system_prompt
    assert created.safety.positive_goals
    assert created.safety.negative_goals
    assert created.safety.guardrails
    assert created.safety.safeguards
    with pytest.raises(ValueError, match="requires.*base_url"):
        ModelConfig(mode="local")
    with pytest.raises(ValueError, match="reasoning"):
        ModelConfig(reasoning="extreme")  # type: ignore[arg-type]
    assert TriggerConfig(hook_path="/custom/incidents/").hook_path == "/custom/incidents"
    with pytest.raises(ValueError, match="absolute route"):
        TriggerConfig(hook_path="custom/{connector}")


def test_environment_template_contains_only_runtime_variables() -> None:
    template = Path(".env.example").read_text(encoding="utf-8")
    variables = {
        line.partition("=")[0]
        for line in template.splitlines()
        if line and not line.startswith("#")
    }
    assert variables == {
        "OPENAI_API_KEY",
        "AGENT_WEBHOOK_SECRET",
        "GITHUB_WEBHOOK_SECRET",
    }


def test_compatible_model_and_safety_configuration_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config = Config(
        model=ModelConfig(
            mode="local",
            provider="ollama",
            base_url="http://127.0.0.1:11434/v1",
            api_key_env="",
        ),
        trigger=TriggerConfig(mode="agent-call", agent_name="resolver"),
        agent=AgentConfig(system_prompt="Resolve incidents safely."),
        safety=SafetyConfig(
            positive_goals=["restore checkout"],
            negative_goals=["do not expose secrets"],
            guardrails=["no production writes"],
            safeguards=["run tests before publishing"],
        ),
    )
    save_config(config, path)
    assert load_config(path) == config


def test_repository_tooling_delegates_to_seed_and_graph_commands(tmp_path: Path) -> None:
    calls: list[tuple[list[str], Path]] = []

    def runner(command, *, cwd, **_kwargs):  # noqa: ANN001, ANN202
        calls.append((command, cwd))
        return subprocess.CompletedProcess(command, 0, "ok\n", "")

    tree = capture_structured_tree(tmp_path, tmp_path / "out" / "structure.seed", runner=runner)
    graphs = build_repository_graphs(tmp_path, runner=runner)

    assert tree.succeeded and all(result.succeeded for result in graphs)
    assert calls[0][0][:2] == ["seed", "capture"]
    assert calls[1][0][:2] == ["graphify", "extract"]
    assert calls[2][0][:2] == ["code-review-graph", "build"]
    assert all(cwd == tmp_path.resolve() for _, cwd in calls)


def test_repository_tooling_validates_paths_and_reports_failures(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        capture_structured_tree(tmp_path / "missing")

    def failing_runner(command, *, cwd, **_kwargs):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess(command, 7, "", "failed")

    result = capture_structured_tree(tmp_path, runner=failing_runner)
    assert not result.succeeded and result.returncode == 7

    def missing_runner(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise FileNotFoundError("missing tool")

    missing = capture_structured_tree(tmp_path, runner=missing_runner)
    assert missing.returncode == 127 and "missing tool" in missing.stderr


def test_github_discovery_and_repository_identifiers(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(command, **_kwargs):  # noqa: ANN001, ANN202
        calls.append(command)
        if command[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(command, 1, "", "not logged in")
        if command[:3] == ["gh", "auth", "login"]:
            return subprocess.CompletedProcess(command, 0, "logged in", "")
        payload = [
            {
                "nameWithOwner": "company/service",
                "url": "https://github.com/company/service",
                "defaultBranchRef": {"name": "trunk"},
            },
            {
                "nameWithOwner": "alpha/api",
                "url": "https://github.com/alpha/api",
                "defaultBranchRef": None,
            },
        ]
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    assert github_login(runner=runner).succeeded
    result, repositories = list_github_repositories(runner=runner)
    assert result.succeeded
    assert [repository.name for repository in repositories] == ["alpha/api", "company/service"]
    assert repositories[0].base_branch == "main"
    assert repositories[1].base_branch == "trunk"
    assert repository_name_from_url("git@github.com:company/service.git") == "company/service"
    assert repository_name_from_url("https://github.com/company/service.git") == "company/service"
    assert repository_slug("company/service") == "company--service"
    with pytest.raises(ValueError, match="owner and repository"):
        repository_name_from_url("service")
    with pytest.raises(ValueError, match="owner/name"):
        repository_slug("../service")

    authenticated = lambda command, **_kwargs: subprocess.CompletedProcess(  # noqa: E731
        command, 0, "authenticated", ""
    )
    assert github_login(runner=authenticated).stdout == "authenticated"

    invalid = lambda command, **_kwargs: subprocess.CompletedProcess(  # noqa: E731
        command, 0, "not-json", ""
    )
    invalid_result, invalid_repositories = list_github_repositories(runner=invalid)
    assert not invalid_result.succeeded and not invalid_repositories

    failed = lambda command, **_kwargs: subprocess.CompletedProcess(  # noqa: E731
        command, 2, "", "offline"
    )
    failed_result, failed_repositories = list_github_repositories(runner=failed)
    assert not failed_result.succeeded and not failed_repositories
    assert tmp_path.is_dir()


def test_clone_pull_and_index_repository(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(command, **_kwargs):  # noqa: ANN001, ANN202
        calls.append(command)
        if command[:2] == ["git", "clone"]:
            target = Path(command[-1])
            target.mkdir(parents=True)
            (target / ".git").mkdir()
        return subprocess.CompletedProcess(command, 0, "ok", "")

    target = tmp_path / "managed" / "company--service"
    setup = clone_and_index_repository(
        "https://github.com/company/service.git", target, runner=runner
    )
    assert setup.succeeded and setup.path == target.resolve()
    assert calls[0][:2] == ["git", "clone"]
    assert calls[1][:2] == ["graphify", "extract"]
    assert "--code-only" in calls[1]
    assert calls[2][:2] == ["code-review-graph", "build"]

    calls.clear()
    refreshed = clone_and_index_repository("unused", target, runner=runner)
    assert refreshed.succeeded and calls[0][-2:] == ["pull", "--ff-only"]

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    with pytest.raises(FileExistsError, match="not a Git checkout"):
        clone_and_index_repository("unused", occupied, runner=runner)

    def failed_clone(command, **_kwargs):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess(command, 3, "", "clone failed")

    failed = clone_and_index_repository("bad", tmp_path / "failed", runner=failed_clone)
    assert not failed.succeeded and failed.graphify is None


def test_storage_creation_dedup_transition_events_and_sessions(
    config: Config, incident: Incident
) -> None:
    storage = Storage(config.runtime_root)
    task = storage.create_task(incident)
    assert storage.create_task(incident).task_id == task.task_id
    assert storage.load_incident(task.task_id) == incident
    active = storage.transition(task.task_id, TaskState.COLLECTING_CONTEXT)
    assert active.state == TaskState.COLLECTING_CONTEXT
    assert storage.task_directory(task.task_id).parent.name == "active"
    storage.append_event(task.task_id, TaskEvent(type="custom", data={"ok": True}))
    assert storage.events(task.task_id)[-1].type == "custom"
    storage.write_artifact(task.task_id, "artifacts/local/test.txt", "passed")
    artifact = storage.task_directory(task.task_id) / "artifacts/local/test.txt"
    assert artifact.read_text() == "passed"
    with pytest.raises(ValueError):
        storage.write_artifact(task.task_id, "../../escape", "bad")
    storage.append_memory("global")
    storage.append_memory("repository", incident.repository)
    assert "global" in storage.read_memory()
    assert "repository" in storage.read_memory(incident.repository)
    storage.add_message(task.conversation_id, "user", "hello")
    assert Storage(config.runtime_root).messages(task.conversation_id) == [("user", "hello")]
    assert storage.find_by_incident("sentry", "INC-1842") is not None
    assert storage.list_tasks("active") == [active]
    with pytest.raises(ValueError):
        storage.task_directory("../bad")
    with pytest.raises(ValueError):
        storage.list_tasks("unknown")
    stale_lock = storage.root / "locks" / "stale.lock"
    stale_lock.write_text("99999999")
    with storage.lock("stale"):
        assert stale_lock.exists()
    assert not stale_lock.exists()
    with (
        storage.lock("live"),
        pytest.raises(FileExistsError, match="lock is held"),
        storage.lock("live"),
    ):
        pass


def test_workspace_tools_are_confined_and_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    tools = WorkspaceTools(workspace, timeout=1, max_output=5)
    tools.write_file("src/example.txt", "old")
    assert tools.read_file("src/example.txt") == "old"
    tools.replace_in_file("src/example.txt", "old", "new")
    assert tools.read_file("src/example.txt") == "new"
    with pytest.raises(ToolError):
        tools.read_file("../secret")
    with pytest.raises(ToolError):
        tools.write_file(".git/config", "bad")
    with pytest.raises(ToolError, match="exactly one"):
        tools.replace_in_file("src/example.txt", "missing", "x")
    monkeypatch.setenv("VERY_SECRET_TOKEN", "hidden")


@pytest.mark.asyncio
async def test_workspace_shell(tmp_path: Path) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    (workspace / "long_output.py").write_text("print('abcdefgh')\n")
    events: list[dict[str, object]] = []
    tools = WorkspaceTools(workspace, max_output=5, logger=events.append)
    result = await tools.shell(f"{sys.executable} long_output.py")
    assert result.returncode == 0 and result.truncated
    assert events[0]["type"] == "tool.shell"
    with pytest.raises(ToolError):
        await tools.shell("sudo whoami")
    with pytest.raises(ToolError):
        await tools.shell("rm -rf target")
    with pytest.raises(ToolError):
        await tools.shell("")
    with pytest.raises(ToolError, match="escapes"):
        await tools.shell("cat ../outside.txt")
    with pytest.raises(ToolError, match="shell operators"):
        await tools.shell("pwd && whoami")
    with pytest.raises(ToolError, match="inline interpreter"):
        await tools.shell(f"{sys.executable} -c 'print(1)'")


@pytest.mark.asyncio
async def test_workspace_tools_enforce_environment_and_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    (workspace / "environment.py").write_text(
        "import os\nprint(os.getenv('OPENAI_API_KEY', 'filtered'))\n"
    )
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    tools = WorkspaceTools(workspace)
    result = await tools.shell(f"{sys.executable} environment.py")
    assert result.stdout.strip() == "filtered"

    read_only = WorkspaceTools(
        workspace,
        permissions=PermissionsConfig(mode="read-only"),
    )
    with pytest.raises(ToolError, match="read-only"):
        read_only.write_file("change.txt", "blocked")
    with pytest.raises(ToolError, match="read-only"):
        await read_only.shell("touch change.txt")

    restricted = WorkspaceTools(
        workspace,
        permissions=PermissionsConfig(
            allow_dependency_installation=False,
            allow_migrations=False,
            allow_ci_modification=False,
            allow_snapshot_updates=False,
        ),
    )
    with pytest.raises(ToolError, match="dependency"):
        await restricted.shell("uv sync")
    with pytest.raises(ToolError, match="migration"):
        await restricted.shell("alembic upgrade head")
    with pytest.raises(ToolError, match="CI"):
        restricted.write_file(".github/workflows/test.yml", "blocked")
    with pytest.raises(ToolError, match="snapshot"):
        restricted.write_file("tests/__snapshots__/view.snap", "blocked")
    with pytest.raises(ToolError, match="migration"):
        restricted.write_file("migrations/0001.sql", "blocked")
    with pytest.raises(ToolError, match="CI"):
        await restricted.shell("sed -i .github/workflows/test.yml")
    with pytest.raises(ToolError, match="snapshot"):
        await restricted.shell("pytest --snapshot-update")
    with pytest.raises(ToolError, match="invalid command"):
        await tools.shell("git 'unterminated")
    with pytest.raises(ToolError, match="absolute command paths"):
        await tools.shell("cat /etc/hosts")
    with pytest.raises(ToolError, match="control directories"):
        await tools.shell("cat .git/config")
    with pytest.raises(ToolError, match="workspace"):
        tools.read_file(str((tmp_path / "outside.txt").resolve()))
    with pytest.raises(ToolError, match="Git"):
        await read_only.shell("git add source.py")
    with pytest.raises(ToolError, match="in-place"):
        await read_only.shell("ruff check --fix source.py")
    missing = await tools.shell("definitely-not-an-installed-command")
    assert missing.returncode == 127 and missing.stderr
    timeout_script = workspace / "timeout.py"
    timeout_script.write_text("import time\ntime.sleep(2)\n")
    with pytest.raises(ToolError, match="timed out"):
        await WorkspaceTools(workspace, timeout=0.01).shell(f"{sys.executable} timeout.py")


def test_github_signature_delivery_and_comment_authorization(config: Config) -> None:
    github = GitHubService(config.github, webhook_secret="secret")
    body = b'{"action":"created"}'
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    github.verify_webhook({"x-hub-signature-256": signature}, body)
    with pytest.raises(WebhookSignatureError):
        github.verify_webhook({"x-hub-signature-256": "bad"}, body)
    assert github.accept_delivery("delivery")
    assert not github.accept_delivery("delivery")
    assert not github.accept_delivery("")
    payload = {
        "repository": {"full_name": "company/application"},
        "pull_request": {"number": 42},
        "comment": {
            "id": 7,
            "body": "Please add a regression test",
            "user": {"login": "maintainer"},
            "author_association": "MEMBER",
        },
    }
    assert github.repository_and_pr(payload) == ("company/application", 42)
    assert github.review_comment(payload).author == "maintainer"  # type: ignore[union-attr]
    payload["comment"]["author_association"] = "NONE"
    assert github.review_comment(payload) is None
    payload["comment"]["author_association"] = "OWNER"
    payload["comment"]["user"]["login"] = config.github.agent_login
    assert github.review_comment(payload) is None
    issue_payload = {**payload, "issue": {"number": 42}}
    issue_payload.pop("pull_request")
    issue_payload["comment"]["user"]["login"] = "owner"
    issue_payload["comment"]["body"] = "ordinary note"
    assert github.review_comment(issue_payload) is None


def test_deployment_matching_rejects_stale_and_wrong_environment(
    config: Config, incident: Incident
) -> None:
    task = Storage(config.runtime_root).create_task(incident)
    task.pr_number = 42
    task.pr_head_sha = "current-sha"
    verifier = DeploymentVerifier(config)
    correct = DeploymentReference(
        repository=task.repository,
        environment="preview",
        sha="current-sha",
        url="https://preview.example.test",
    )
    assert verifier.accepts(task, correct)
    assert not verifier.accepts(task, correct.model_copy(update={"sha": "stale"}))
    assert not verifier.accepts(task, correct.model_copy(update={"environment": "production"}))
    assert not verifier.accepts(task, correct.model_copy(update={"url": "file:///tmp/site"}))


@pytest.mark.asyncio
async def test_verifier_runs_command_against_preview(
    config: Config, incident: Incident, tmp_path: Path
) -> None:
    repository = config.repositories[0]
    repository.playwright.command = (
        f"{sys.executable} -c 'import os; assert os.environ[\"BASE_URL\"]'"
    )
    repository.playwright.base_url_env = "BASE_URL"
    storage = Storage(config.runtime_root)
    task = storage.create_task(incident)
    task.pr_number, task.pr_head_sha = 42, "sha"
    deployment = DeploymentReference(
        repository=task.repository,
        environment="preview",
        sha="sha",
        url="https://preview.example.test",
    )
    transport = httpx.MockTransport(lambda _: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as client:
        verifier = DeploymentVerifier(config, client=client)
        result = await verifier.verify(task, deployment, tmp_path)
    assert result.passed and result.url == deployment.url
    stale = await verifier.verify(task, deployment.model_copy(update={"sha": "old"}), tmp_path)
    assert not stale.passed and "does not match" in (stale.reason or "")


@pytest.mark.asyncio
async def test_verifier_retries_configured_playwright_command(
    config: Config, incident: Incident, tmp_path: Path
) -> None:
    attempts = tmp_path / "attempts.txt"
    script = tmp_path / "flaky_verification.py"
    script.write_text(
        "from pathlib import Path\n"
        f"path = Path({str(attempts)!r})\n"
        "attempt = int(path.read_text()) + 1 if path.exists() else 1\n"
        "path.write_text(str(attempt))\n"
        "raise SystemExit(0 if attempt == 2 else 1)\n"
    )
    repository = config.repositories[0]
    repository.playwright.command = f"{sys.executable} {script}"
    repository.playwright.retries = 1
    task = Storage(config.runtime_root).create_task(incident)
    task.pr_number, task.pr_head_sha = 42, "sha"
    deployment = DeploymentReference(
        repository=task.repository,
        environment="preview",
        sha="sha",
        url="https://preview.example.test",
    )
    transport = httpx.MockTransport(lambda _: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await DeploymentVerifier(config, client=client).verify(task, deployment, tmp_path)
    assert result.passed
    assert attempts.read_text() == "2"
    assert "Attempt 1/2" in result.output and "Attempt 2/2" in result.output
    repository.playwright.command = f"{sys.executable} -c 'raise SystemExit(2)'"
    repository.playwright.retries = 0
    async with httpx.AsyncClient(transport=transport) as client:
        failed = await DeploymentVerifier(config, client=client).verify(task, deployment, tmp_path)
    assert not failed.passed and failed.reason == "Playwright command failed"


class FakeAgent:
    async def investigate(self, task, worktree):  # noqa: ANN001, ANN201
        return InvestigationResult(
            root_cause="nullable session", evidence=["stack trace"], proposed_fix="guard access"
        )

    async def implement_fix(self, task, worktree):  # noqa: ANN001, ANN201
        return FixResult(changed=True, summary="added guard", tests_passed=True)

    async def address_review(self, task, comments, worktree):  # noqa: ANN001, ANN201
        return ReviewResult(
            changed=True, summary="addressed", tests_passed=True, head_sha="review-sha"
        )


class FakeGitHub(GitHubService):
    async def create_pull_request(self, task):  # noqa: ANN001, ANN201
        return PullRequestReference(
            repository=task.repository,
            number=42,
            url="https://github.test/pr/42",
            head_sha="head-sha",
            branch=task.branch or "agent/fix",
        )

    async def publish_verification(self, task, result):  # noqa: ANN001, ANN201
        self.published = result


class FakeVerifier(DeploymentVerifier):
    async def verify(self, task, deployment, worktree):  # noqa: ANN001, ANN201
        return VerificationResult(
            passed=True,
            environment=deployment.environment,
            sha=deployment.sha,
            url=deployment.url,
            output="1 passed",
        )


@pytest.mark.asyncio
async def test_workflow_lifecycle_event_routing_and_restart_recovery(
    config: Config, incident: Incident
) -> None:
    config.repositories[0].publish_mode = "github"
    storage = Storage(config.runtime_root)
    github = FakeGitHub(config.github, webhook_secret="secret")
    verifier = FakeVerifier(config)
    workflow = WorkflowEngine(config, storage, FakeAgent(), github, verifier)  # type: ignore[arg-type]
    task = await workflow.submit(incident)
    (storage.root / "worktrees" / task.task_id).mkdir(parents=True)
    assert (await workflow.submit(incident)).task_id == task.task_id
    for expected in (
        TaskState.COLLECTING_CONTEXT,
        TaskState.INVESTIGATING,
        TaskState.REPRODUCING,
        TaskState.IMPLEMENTING,
        TaskState.TESTING_LOCAL,
        TaskState.PUBLISHING_PR,
        TaskState.WAITING_FOR_DEPLOYMENT,
    ):
        task = await workflow.process(task.task_id)
        assert task.state == expected
    payload = {
        "repository": {"full_name": task.repository},
        "pull_request": {"number": 42},
        "deployment": {"id": 9, "environment": "preview", "sha": "head-sha"},
        "deployment_status": {
            "state": "success",
            "environment_url": "https://preview.example.test",
        },
    }
    task = await workflow.handle_github_event("deployment_status", payload)
    assert task and task.state == TaskState.TESTING_DEPLOYMENT
    task = await workflow.process(task.task_id)
    assert task.state == TaskState.WAITING_FOR_REVIEW
    review = {
        "action": "created",
        "repository": {"full_name": task.repository},
        "pull_request": {"number": 42},
        "comment": {
            "id": 8,
            "body": "Please rename this",
            "user": {"login": "owner"},
            "author_association": "OWNER",
        },
    }
    await workflow.handle_github_event("pull_request_review_comment", review)
    task = await workflow.process(task.task_id)
    assert task.state == TaskState.IMPLEMENTING and task.pr_head_sha == "review-sha"
    assert (await workflow.process(task.task_id)).state == TaskState.TESTING_LOCAL
    assert (await workflow.process(task.task_id)).state == TaskState.WAITING_FOR_DEPLOYMENT
    # A process restart discovers durable pending/active work.
    second = storage.create_task(incident.model_copy(update={"external_id": "INC-2"}))
    recovered = WorkflowEngine(config, storage, FakeAgent(), github, verifier)  # type: ignore[arg-type]
    await recovered.recover()
    assert await recovered._wakeups.get() == second.task_id


@pytest.mark.asyncio
async def test_workflow_merge_cancel_and_rejections(config: Config, incident: Incident) -> None:
    storage = Storage(config.runtime_root)
    github = FakeGitHub(config.github, webhook_secret="secret")
    workflow = WorkflowEngine(
        config,
        storage,
        FakeAgent(),
        github,
        FakeVerifier(config),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="not configured"):
        await workflow.submit(incident.model_copy(update={"repository": "other/repo"}))
    with pytest.raises(ValueError, match="not enabled"):
        await workflow.submit(incident.model_copy(update={"environment": "development"}))
    task = storage.create_task(incident)
    task.pr_number = 42
    storage.save_task(task)
    merged = await workflow.handle_github_event(
        "pull_request",
        {
            "action": "closed",
            "repository": {"full_name": task.repository},
            "pull_request": {"number": 42, "merged": True},
        },
    )
    assert merged and merged.state == TaskState.COMPLETED
    assert workflow.cancel(merged.task_id).state == TaskState.COMPLETED
    other = storage.create_task(incident.model_copy(update={"external_id": "INC-3"}))
    assert workflow.cancel(other.task_id).state == TaskState.CANCELLED


def test_server_health_submission_resources_and_github_security(
    config: Config, incident: Incident
) -> None:
    storage = Storage(config.runtime_root)
    connectors = ConnectorManager(config.connectors)
    github = FakeGitHub(config.github, webhook_secret="secret")
    verifier = FakeVerifier(config)
    agent = IncidentAgent(config, storage, connectors)
    workflow = WorkflowEngine(config, storage, agent, github, verifier)
    application = Application(config, storage, connectors, github, agent, verifier, workflow)
    server = create_server(application, run_worker=False)
    with TestClient(server) as client:
        assert client.get("/health").json() == {"status": "ok"}
        response = client.post("/mcp/tools/submit_incident", json=incident.model_dump(mode="json"))
        assert response.status_code == 200
        task_id = response.json()["task_id"]
        assert client.get(f"/mcp/resources/tasks/{task_id}").status_code == 200
        events = client.get(f"/mcp/resources/tasks/{task_id}/events").json()
        assert events[0]["type"] == "task.received"
        assert client.get(f"/mcp/resources/tasks/{task_id}/result").json()["result"] is None
        assert client.get("/.well-known/agent-card.json").json()["name"] == "Incident Harness"
        assert client.get("/a2a/tasks/missing").status_code == 404
        assert client.post(f"/a2a/tasks/{task_id}/cancel").json()["state"] == "cancelled"
        assert client.post("/hooks/github", content=b"{}").status_code == 401
        body = json.dumps({"zen": "secure"}).encode()
        signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        headers = {
            "x-hub-signature-256": signature,
            "x-github-delivery": "delivery-1",
            "x-github-event": "ping",
        }
        assert client.post("/hooks/github", content=body, headers=headers).status_code == 202
        duplicate = client.post("/hooks/github", content=body, headers=headers)
        assert duplicate.json() == {"duplicate": True}
        submitted = client.post("/hooks/incidents/sentry", json=incident.model_dump(mode="json"))
        assert submitted.status_code == 202
        assert client.post("/hooks/incidents/missing", json={}).status_code == 422


def test_cli_parsing() -> None:
    assert parse_arguments(["serve", "--no-worker"]).no_worker
    assert parse_arguments(["worker"]).command == "worker"
    assert parse_arguments(["run", "incident.json"]).incident == Path("incident.json")


def test_repository_graph_and_structure_adapters(tmp_path: Path) -> None:
    calls: list[tuple[list[str], Path]] = []

    def runner(command, *, cwd, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls.append((command, cwd))
        return subprocess.CompletedProcess(command, 0, "ok", "")

    result = capture_structured_tree(tmp_path, tmp_path / "structure.seed", runner=runner)
    assert result.succeeded and calls[0][0][:2] == ["seed", "capture"]
    graphify, review_graph = build_repository_graphs(tmp_path, runner=runner)
    assert graphify.succeeded and review_graph.succeeded and len(calls) == 3
    with pytest.raises(NotADirectoryError):
        capture_structured_tree(tmp_path / "missing", runner=runner)
    assert parse_arguments(["index", "repo"]).path == Path("repo")
    assert parse_arguments(["tree", "repo", "--out", "repo.seed"]).out == Path("repo.seed")


def test_builtin_skill_manifest_and_routing_are_complete() -> None:
    manifest = json.loads(Path("skills/manifest.json").read_text())
    entries = manifest["skills"]
    expected = {
        "coding",
        "deployment-verification",
        "github",
        "graphify",
        "incident-investigation",
        "review-comments",
        "testing",
    }
    assert {entry["name"] for entry in entries} == expected
    resolver = Path("AGENTS.md").read_text()
    for entry in entries:
        skill_path = Path("skills") / entry["path"]
        content = skill_path.read_text()
        assert "\ntriggers:\n" in content
        assert f"`{skill_path}`" in resolver


class FakeSession:
    def __init__(self) -> None:
        self.closed = False
        self.tools = ["logs-tool"]

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_connector_lifecycle_tools_testing_and_normalization(
    config: Config, incident: Incident
) -> None:
    session = FakeSession()

    async def factory(_config):  # noqa: ANN001, ANN202
        return session

    config.connectors[0].capabilities = ["logs"]
    manager = ConnectorManager(config.connectors, {"sentry": factory})
    await manager.start()
    assert await manager.tools_for({"logs"}) == ["logs-tool"]
    assert (await manager.test_connection("sentry")).connected
    assert not (await manager.test_connection("missing")).connected
    normalized = manager.normalize_incident(
        "sentry",
        {
            "external_id": incident.external_id,
            "repository": incident.repository,
            "environment": incident.environment,
            "summary": incident.summary,
            "evidence": [{"kind": "log", "content": "failure"}],
        },
    )
    assert normalized.evidence[0].kind == "log"
    with pytest.raises(KeyError):
        manager.normalize_incident("missing", {})
    with pytest.raises(ValueError, match="missing incident fields"):
        manager.normalize_incident("sentry", {})
    await manager.stop()
    assert session.closed and not manager.sessions


@pytest.mark.asyncio
async def test_connector_failure_and_callable_tools(config: Config) -> None:
    class CallableSession:
        async def tools(self):  # noqa: ANN201
            return ["metrics-tool"]

        def close(self) -> None:
            pass

    async def factory(_config):  # noqa: ANN001, ANN202
        return CallableSession()

    async def broken(_config):  # noqa: ANN001, ANN202
        raise RuntimeError("offline")

    config.connectors[0].capabilities = ["metrics"]
    manager = ConnectorManager(config.connectors, {"sentry": factory})
    await manager.start()
    assert await manager.tools_for({"metrics"}) == ["metrics-tool"]
    await manager.stop()
    assert not (await ConnectorManager(config.connectors).test_connection("sentry")).connected
    result = await ConnectorManager(config.connectors, {"sentry": broken}).test_connection("sentry")
    assert not result.connected and result.message == "offline"

    failing_manager = ConnectorManager(config.connectors, {"sentry": broken})
    await failing_manager.start()
    assert failing_manager.errors == {"sentry": "offline"}
    assert not (await failing_manager.test_connection("sentry")).connected


@pytest.mark.asyncio
async def test_default_mcp_connector_transports(monkeypatch: pytest.MonkeyPatch) -> None:
    import agents.mcp as agents_mcp

    created: list[tuple[str, dict[str, object], str]] = []

    class FakeMCPServer:
        def __init__(self, transport: str, params: dict[str, object], name: str) -> None:
            created.append((transport, params, name))
            self.connected = False
            self.cleaned = False

        async def connect(self) -> None:
            self.connected = True

        async def cleanup(self) -> None:
            self.cleaned = True

        async def list_tools(self):  # noqa: ANN201
            return []

        async def call_tool(self):  # noqa: ANN201
            return None

    def factory(transport):  # noqa: ANN001, ANN202
        return lambda params, name, **_kwargs: FakeMCPServer(transport, params, name)

    monkeypatch.setattr(agents_mcp, "MCPServerStdio", factory("stdio"))
    monkeypatch.setattr(agents_mcp, "MCPServerStreamableHttp", factory("http"))
    monkeypatch.setattr(agents_mcp, "MCPServerSse", factory("sse"))
    monkeypatch.setenv("MCP_TOKEN", "secret")

    stdio = ConnectorConfig(
        name="local", transport="stdio", command=["server", "--stdio"], capabilities=["logs"]
    )
    http = ConnectorConfig(
        name="remote",
        transport="streamable-http",
        url="https://mcp.test",
        auth_token_env="MCP_TOKEN",
        capabilities=["logs"],
    )
    sse = ConnectorConfig(name="events", transport="sse", url="https://sse.test")
    for connector in (stdio, http, sse):
        server = await ConnectorManager._mcp_factory(connector)  # noqa: SLF001
        assert server.connected
        await ConnectorManager._close(server)  # noqa: SLF001
        assert server.cleaned

    assert created[0][1] == {"command": "server", "args": ["--stdio"]}
    assert created[1][1]["headers"] == {"Authorization": "Bearer secret"}
    assert created[2][0] == "sse"

    manager = ConnectorManager([http], {"remote": lambda _config: _connected_server(created)})
    await manager.start()
    assert await manager.tools_for({"logs"}) == [manager.sessions["remote"]]
    await manager.stop()

    monkeypatch.delenv("MCP_TOKEN")
    with pytest.raises(RuntimeError, match="MCP_TOKEN"):
        await ConnectorManager._mcp_factory(http)  # noqa: SLF001


async def _connected_server(_created):  # noqa: ANN001, ANN202
    class ConnectedServer:
        async def connect(self):  # noqa: ANN201
            return None

        async def list_tools(self):  # noqa: ANN201
            return []

        async def call_tool(self):  # noqa: ANN201
            return None

    return ConnectedServer()


@pytest.mark.asyncio
async def test_agent_context_and_all_entry_points(config: Config, incident: Incident) -> None:
    config.agent.system_prompt = "Follow the incident resolution policy."
    config.safety.positive_goals = ["restore the service"]
    config.safety.negative_goals = ["do not expose secrets"]
    config.safety.guardrails = ["no production writes"]
    config.safety.safeguards = ["run tests before publishing"]
    storage = Storage(config.runtime_root)
    task = storage.create_task(incident)
    worktree = storage.root / "worktrees" / task.task_id
    worktree.mkdir(parents=True)
    (worktree / "AGENTS.md").write_text("repository rules")
    storage.append_memory("remember this")
    calls: list[str] = []

    async def backend(instructions, prompt, tools, connector_tools):  # noqa: ANN001, ANN202
        calls.append(instructions + prompt)
        if len(calls) == 1:
            return {"root_cause": "bug", "evidence": ["trace"], "proposed_fix": "fix"}
        if len(calls) == 2:
            return {"changed": True, "summary": "fixed", "tests_passed": True}
        return {"changed": False, "summary": "answered", "tests_passed": True}

    agent = IncidentAgent(config, storage, ConnectorManager([]), backend)
    assert (await agent.investigate(task, worktree)).root_cause == "bug"
    storage.write_artifact(task.task_id, "investigation.md", "investigation")
    assert (await agent.implement_fix(task, worktree)).changed
    comment = GitHubService(config.github).review_comment(
        {
            "comment": {
                "id": 1,
                "body": "why?",
                "user": {"login": "owner"},
                "author_association": "OWNER",
            }
        }
    )
    assert comment and not (await agent.address_review(task, [comment], worktree)).changed
    assert "repository rules" in calls[0] and "remember this" in calls[0]
    assert "Follow the incident resolution policy." in calls[0]
    assert "Binding Safety Contract" in calls[0]
    assert "restore the service" in calls[0]
    assert "do not expose secrets" in calls[0]
    assert "no production writes" in calls[0]
    assert "run tests before publishing" in calls[0]
    assert "Required Repository Graph Check" in calls[0]
    assert "# /graphify" in calls[0]
    assert "# Incident Investigation" in calls[0]
    assert "# Coding" in calls[1] and "# Testing" in calls[1] and "# GitHub" in calls[1]
    assert "# Review Comments" in calls[2]
    assert len(storage.messages(task.conversation_id)) == 6


@pytest.mark.asyncio
async def test_agent_preloads_both_fresh_repository_graphs(
    config: Config, incident: Incident, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage(config.runtime_root)
    task = storage.create_task(incident)
    worktree = storage.root / "worktrees" / task.task_id
    (worktree / "graphify-out").mkdir(parents=True)
    (worktree / "graphify-out" / "graph.json").write_text("{}")
    (worktree / ".code-review-graph").mkdir()
    (worktree / ".code-review-graph" / "graph.db").write_bytes(b"")
    shell = AsyncMock(return_value=CommandResult("graphify", 0, "graph route", ""))

    import code_review_graph.tools as graph_tools

    monkeypatch.setattr(
        graph_tools,
        "semantic_search_nodes",
        lambda **values: {"summary": "code route", "query": values["query"]},
    )
    context = await IncidentAgent._graph_context(  # noqa: SLF001
        task, worktree, types.SimpleNamespace(shell=shell)
    )
    assert "graph route" in context and "code route" in context
    assert incident.summary in shell.await_args.args[0]

    shell.return_value = CommandResult("graphify", 2, "", "query failed")
    assert "query failed" in await IncidentAgent._graph_context(  # noqa: SLF001
        task, worktree, types.SimpleNamespace(shell=shell)
    )
    (worktree / "graphify-out" / "graph.json").unlink()
    (worktree / ".code-review-graph" / "graph.db").unlink()
    assert not await IncidentAgent._graph_context(  # noqa: SLF001
        task, worktree, types.SimpleNamespace(shell=shell)
    )


@pytest.mark.asyncio
async def test_default_agents_backend(config: Config, tmp_path: Path, monkeypatch) -> None:
    assert OpenAIAgentsBackend(Config())._model(None) == "gpt-5"  # noqa: SLF001
    config.model = ModelConfig(
        mode="local",
        provider="ollama",
        base_url="http://127.0.0.1:11434/v1",
        api_key_env="",
        name="local-model",
    )
    workspace = WorkspaceTools(tmp_path)
    workspace.shell = AsyncMock(
        side_effect=lambda command: CommandResult(
            command,
            0,
            "graph result" if command.startswith("graphify query") else str(tmp_path),
            "",
        )
    )

    import code_review_graph.tools as graph_tools

    monkeypatch.setattr(
        graph_tools,
        "semantic_search_nodes",
        lambda **values: {"operation": "search", **values},
    )
    monkeypatch.setattr(
        graph_tools,
        "query_graph",
        lambda **values: {"operation": "query", **values},
    )
    monkeypatch.setattr(
        graph_tools,
        "get_impact_radius",
        lambda **values: {"operation": "impact", **values},
    )

    class MCPServer:
        async def connect(self):  # noqa: ANN201
            return None

        async def list_tools(self):  # noqa: ANN201
            return []

        async def call_tool(self):  # noqa: ANN201
            return None

    mcp_server = MCPServer()
    connector_tool = object()

    class FakeClient:
        def __init__(self, **values):  # noqa: ANN003
            assert values["api_key"] == "local"

    class FakeModel(dict):
        def __init__(self, **values):  # noqa: ANN003
            super().__init__(values)

    class SDKAgent:
        def __init__(self, **values):  # noqa: ANN003
            self.__dict__.update(values)
            assert isinstance(self.model_settings, ModelSettings)
            assert self.model_settings.tool_choice == "required"
            assert self.model_settings.parallel_tool_calls is False
            assert self.model_settings.reasoning.effort == "high"
            assert self.model["model"] == "local-model"
            assert self.reset_tool_choice is True
            assert self.mcp_servers in ([mcp_server], [])

    class SDKRunner:
        outputs: list[object] = ['{"changed": true}']

        @staticmethod
        async def run(agent, prompt, max_turns):  # noqa: ANN001, ANN202
            if not (tmp_path / "example.txt").exists():
                shell, read, write, replace, graphify, search, query, impact, connector = (
                    agent.tools
                )
                assert connector is connector_tool
                assert write("example.txt", "old") == "written"
                assert read("example.txt") == "old"
                assert replace("example.txt", "old", "new") == "replaced"
                assert "returncode" in await shell("pwd")
                assert await graphify("checkout failure") == "graph result"
                assert json.loads(search("checkout", "Function", 3))["operation"] == "search"
                assert json.loads(query("callers_of", "checkout"))["operation"] == "query"
                assert json.loads(impact(["checkout.py"], 3))["operation"] == "impact"
            assert prompt == "resolve" and max_turns == config.model.max_turns_per_iteration
            return types.SimpleNamespace(final_output=SDKRunner.outputs.pop(0))

    fake_agents = types.SimpleNamespace(
        Agent=SDKAgent,
        AsyncOpenAI=FakeClient,
        ModelSettings=ModelSettings,
        OpenAIChatCompletionsModel=FakeModel,
        Runner=SDKRunner,
        function_tool=lambda function: function,
    )
    monkeypatch.setitem(sys.modules, "agents", fake_agents)
    backend = OpenAIAgentsBackend(config)
    result = await backend("instructions", "resolve", workspace, [mcp_server, connector_tool])
    assert result == {"changed": True}

    SDKRunner.outputs = [{"changed": True}, 3, "not json", "[]"]
    assert await backend("instructions", "resolve", workspace, []) == {"changed": True}
    with pytest.raises(RuntimeError, match="non-JSON"):
        await backend("instructions", "resolve", workspace, [])
    with pytest.raises(RuntimeError, match="invalid JSON"):
        await backend("instructions", "resolve", workspace, [])
    with pytest.raises(RuntimeError, match="must be an object"):
        await backend("instructions", "resolve", workspace, [])


def test_openai_compatible_model_uses_configured_endpoint_and_env(monkeypatch) -> None:
    config = Config(
        model=ModelConfig(
            mode="remote",
            provider="compatible-cloud",
            base_url="https://models.example/v1",
            api_key_env="COMPATIBLE_API_KEY",
            organization_env="COMPATIBLE_ORG",
            name="compatible-model",
        )
    )
    monkeypatch.setenv("COMPATIBLE_API_KEY", "secret-is-not-written")
    monkeypatch.setenv("COMPATIBLE_ORG", "org-test")
    calls: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, **values):  # noqa: ANN003
            calls.append(values)

    def fake_model(**values):  # noqa: ANN003
        return values

    backend = OpenAIAgentsBackend(config)
    result = backend._model(  # noqa: SLF001
        types.SimpleNamespace(AsyncOpenAI=FakeClient, OpenAIChatCompletionsModel=fake_model)
    )
    assert calls == [
        {
            "api_key": "secret-is-not-written",
            "organization": "org-test",
            "base_url": "https://models.example/v1",
        }
    ]
    assert result["model"] == "compatible-model"
    assert result["openai_client"] is backend._compatible_client  # noqa: SLF001


@pytest.mark.asyncio
async def test_github_injected_operations(config: Config, incident: Incident) -> None:
    task = Storage(config.runtime_root).create_task(incident)
    calls: list[str] = []

    async def api(operation, payload):  # noqa: ANN001, ANN202
        calls.append(operation)
        if operation == "create_pull_request":
            return {
                "repository": task.repository,
                "number": 1,
                "url": "https://github.test/1",
                "head_sha": "sha",
                "branch": "agent/fix",
            }
        if operation == "get_review_threads":
            return [{"id": 1, "body": "fix", "author": "owner"}]
        return None

    github = GitHubService(config.github, api=api)
    assert (await github.create_pull_request(task)).number == 1
    comments = await github.get_review_threads(task)
    await github.reply_to_review(comments[0], "done")
    await github.publish_verification(
        task,
        VerificationResult(passed=True, environment="preview", sha="sha", url="https://x"),
    )
    assert calls == [
        "create_pull_request",
        "get_review_threads",
        "reply_to_review",
        "publish_verification",
    ]
    with pytest.raises(RuntimeError, match="not configured"):
        await GitHubService(config.github).create_pull_request(task)
    with pytest.raises(WebhookSignatureError, match="not configured"):
        GitHubService(config.github).verify_webhook({}, b"")
    with pytest.raises(ValueError):
        GitHubService.decode(b"[]")
    assert GitHubService.repository_and_pr({}) is None


@pytest.mark.asyncio
async def test_verifier_lookup_unreachable_and_unconfigured(
    config: Config, incident: Incident, tmp_path: Path
) -> None:
    storage = Storage(config.runtime_root)
    task = storage.create_task(incident)
    task.pr_number, task.pr_head_sha = 1, "sha"
    stale = DeploymentReference(
        repository=task.repository,
        environment="preview",
        sha="old",
        url="https://preview.test",
    )
    current = stale.model_copy(update={"sha": "sha"})

    async def source(_task):  # noqa: ANN001, ANN202
        return [stale, current]

    verifier = DeploymentVerifier(config, deployment_source=source)
    assert await verifier.find_current_deployment(task) == current
    assert await DeploymentVerifier(config).find_current_deployment(task) is None
    transport = httpx.MockTransport(lambda _: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await DeploymentVerifier(config, client=client).verify(task, current, tmp_path)
    assert not result.passed and "not configured" in (result.reason or "")


@pytest.mark.asyncio
async def test_tui_save(config: Config, tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    save_config(config, path)
    app = ConfigurationApp(path)
    async with app.run_test() as pilot:
        model_input = app.query_one("#model", Input)
        model_input.scroll_visible()
        await pilot.pause()
        await pilot.click("#model")
        await pilot.press("end", "ctrl+u", *"new-model")
        assert model_input.value == "new-model"
        model_sections = [widget.region for widget in app.query("#model-tab .section")]
        assert all(
            current.y + current.height <= following.y
            for current, following in zip(model_sections, model_sections[1:], strict=False)
        )
        app.query_one("#model-mode", Select).value = "local"
        app.query_one("#model-base-url").value = "http://localhost:11434/v1"
        app.query_one("#model-temperature").value = "0.2"
        app.query_one("#model-top-p").value = "0.9"
        app.query_one("#model-max-tokens").value = "2048"
        app.query_one("#model-parallel-tools").value = True
        app.query_one("#model-max-turns").value = "12"
        app.query_one("#model-max-iterations").value = "3"
        app.query_one("#model-tool-timeout").value = "90"
        app.query_one(TabbedContent).active = "runtime-tab"
        await pilot.pause()
        runtime_input = app.query_one("#runtime-root", Input)
        runtime_input.scroll_visible()
        await pilot.pause()
        runtime_input.focus()
        await pilot.pause()
        await pilot.press("end", "ctrl+u", *str(tmp_path / "runtime"))
        assert runtime_input.value == str(tmp_path / "runtime")
        runtime_sections = [widget.region for widget in app.query("#runtime-tab .section")]
        assert all(
            current.y + current.height <= following.y
            for current, following in zip(runtime_sections, runtime_sections[1:], strict=False)
        )
        app.query_one("#max-concurrent-tasks").value = "4"
        app.query_one("#worker-poll-interval").value = "0.25"
        app.query_one("#host").value = "0.0.0.0"
        app.query_one("#port").value = "9876"
        app.query_one("#positive-goals", TextArea).text = "restore service\npass tests"
        app.query_one("#system-prompt", TextArea).text = "Use the configured incident policy."
        app.query_one("#save", Button).press()
        await pilot.pause()
    saved = load_config(path)
    assert saved.model.name == "new-model"
    assert saved.model.mode == "local"
    assert saved.model.base_url == "http://localhost:11434/v1"
    assert saved.model.temperature == 0.2
    assert saved.model.top_p == 0.9
    assert saved.model.max_tokens == 2048
    assert saved.model.parallel_tool_calls is True
    assert saved.model.max_turns_per_iteration == 12
    assert saved.model.max_task_iterations == 3
    assert saved.model.tool_timeout_seconds == 90
    assert saved.runtime_root == tmp_path / "runtime"
    assert saved.max_concurrent_tasks == 4
    assert saved.poll_interval_seconds == 0.25
    assert saved.server.host == "0.0.0.0"
    assert saved.server.port == 9876
    assert saved.safety.positive_goals == ["restore service", "pass tests"]
    assert saved.agent.system_prompt == "Use the configured incident policy."


@pytest.mark.asyncio
async def test_tui_reports_incomplete_local_model_without_crashing(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    app = ConfigurationApp(path)
    async with app.run_test() as pilot:
        app.query_one("#model-mode", Select).value = "local"
        app.query_one("#save", Button).press()
        await pilot.pause()

        status = app.query_one("#status").render()
        assert "local model mode requires an OpenAI-compatible base_url" in status.plain

        app.query_one("#model-base-url").value = "http://localhost:11434/v1"
        app.query_one("#save", Button).press()
        await pilot.pause()

    assert load_config(path).model.mode == "local"


@pytest.mark.asyncio
async def test_tui_add_connector_does_not_validate_incomplete_draft(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    app = ConfigurationApp(path)
    async with app.run_test() as pilot:
        assert app.query_one("#runtime-root").value == str(tmp_path)
        assert app.query_one("#max-concurrent-tasks").value == "2"

        app.query_one(TabbedContent).active = "connections-tab"
        await pilot.pause()
        app.query_one("#add-connector", Button).press()
        await pilot.pause()

        app.query_one("#connector-connector-0-name").value = "logs"
        app.query_one("#connector-connector-0-url").value = "https://mcp.example.test"
        app.query_one("#save", Button).press()
        await pilot.pause()

    saved = load_config(path)
    assert saved.connectors[0].name == "logs"
    assert saved.connectors[0].url == "https://mcp.example.test"


@pytest.mark.asyncio
async def test_tui_github_repository_setup_and_connection_test(tmp_path: Path) -> None:
    state = {"mode": "good"}
    calls: list[list[str]] = []

    def runner(command, **_kwargs):  # noqa: ANN001, ANN202
        calls.append(command)
        mode = state["mode"]
        if command[:3] == ["gh", "auth", "status"]:
            code = 1 if mode == "login-failure" else 0
            return subprocess.CompletedProcess(command, code, "", "login failed" if code else "")
        if command[:3] == ["gh", "auth", "login"]:
            return subprocess.CompletedProcess(command, 1, "", "browser login failed")
        if command[:3] == ["gh", "repo", "list"]:
            if mode == "list-failure":
                return subprocess.CompletedProcess(command, 2, "", "GitHub unavailable")
            payload = [
                {
                    "nameWithOwner": "company/application",
                    "url": "https://github.com/company/application",
                    "defaultBranchRef": {"name": "trunk"},
                }
            ]
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if command[:2] == ["git", "clone"]:
            if mode == "clone-failure":
                return subprocess.CompletedProcess(command, 3, "", "clone failed")
            target = Path(command[-1])
            target.mkdir(parents=True, exist_ok=True)
            (target / ".git").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(command, 0, "cloned", "")
        if command[:2] == ["graphify", "extract"] and mode == "graph-failure":
            return subprocess.CompletedProcess(command, 4, "", "graph failed")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    path = tmp_path / "config.toml"
    app = ConfigurationApp(path, command_runner=runner)
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "repositories-tab"
        await pilot.pause()
        app.query_one("#add-repository", Button).press()
        await pilot.pause()
        assert app.query_one("#repo-repository-0-name", Input).region.height > 0

        source = app.query_one("#repo-repository-0-source", Select)
        source.value = "github"
        await pilot.pause()
        assert app.query_one("#github-login-repository-0", Button).disabled is False
        assert app.query_one("#repo-repository-0-clone-url", Input).disabled is True

        app.query_one("#github-login-repository-0", Button).press()
        await pilot.pause(0.05)
        github_repository = app.query_one("#repo-repository-0-github-repository", Select)
        github_repository.value = "company/application"
        await pilot.pause()
        assert app.query_one("#repo-repository-0-name", Input).value == "company/application"
        assert app.query_one("#repo-repository-0-base-branch", Input).value == "trunk"

        app.query_one("#setup-repository-0", Button).press()
        await pilot.pause(0.1)
        status = app.query_one("#repo-status-repository-0", Static).render().plain
        assert "pulled, indexed, and saved" in status
        saved = load_config(path)
        assert saved.repositories[0].name == "company/application"
        assert Path(saved.repositories[0].local_path or "").is_dir()
        assert any(command[:2] == ["graphify", "extract"] for command in calls)
        assert any(command[:2] == ["code-review-graph", "build"] for command in calls)

        app.query_one(TabbedContent).active = "connections-tab"
        await pilot.pause()
        app.query_one("#add-connector", Button).press()
        await pilot.pause()
        app.query_one("#test-connector-1", Button).press()
        await pilot.pause()
        connector_status = app.query_one("#connector-status-connector-1", Static).render().plain
        assert "Invalid connection" in connector_status

        app.query_one("#connector-connector-1-url", Input).value = "https://mcp.test"
        with patch.object(
            ConnectorManager,
            "test_connection",
            AsyncMock(return_value=ConnectorTestResult("new-connection", True, "connected")),
        ):
            app.query_one("#test-connector-1", Button).press()
            await pilot.pause()
        connector_status = app.query_one("#connector-status-connector-1", Static).render().plain
        assert connector_status == "Connected: connected"

        app.query_one("#remove-connector-1", Button).press()
        app.query_one("#remove-repository-0", Button).press()
        await pilot.pause()
        assert not app.query("#connector-1") and not app.query("#repository-0")


@pytest.mark.asyncio
async def test_tui_repository_and_github_failures_are_reported(tmp_path: Path) -> None:
    state = {"mode": "login-failure"}

    def runner(command, **_kwargs):  # noqa: ANN001, ANN202
        mode = state["mode"]
        if command[:3] == ["gh", "auth", "status"]:
            code = 0 if mode != "login-failure" else 1
            return subprocess.CompletedProcess(command, code, "", "not authenticated")
        if command[:3] == ["gh", "auth", "login"]:
            return subprocess.CompletedProcess(command, 1, "", "login failed")
        if command[:3] == ["gh", "repo", "list"]:
            return subprocess.CompletedProcess(command, 2, "", "list failed")
        if command[:2] == ["git", "clone"]:
            if mode == "clone-failure":
                return subprocess.CompletedProcess(command, 3, "", "clone failed")
            target = Path(command[-1])
            target.mkdir(parents=True, exist_ok=True)
            (target / ".git").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["graphify", "extract"]:
            return subprocess.CompletedProcess(command, 4, "", "graph failed")
        return subprocess.CompletedProcess(command, 0, "", "")

    app = ConfigurationApp(tmp_path / "config.toml", command_runner=runner)
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "repositories-tab"
        app.query_one("#add-repository", Button).press()
        await pilot.pause()
        key = "repository-0"
        await app._load_github_repositories(key)  # noqa: SLF001
        assert "login failed" in app.query_one(f"#repo-status-{key}", Static).render().plain

        state["mode"] = "list-failure"
        await app._load_github_repositories(key)  # noqa: SLF001
        assert "list failed" in app.query_one(f"#repo-status-{key}", Static).render().plain

        await app._setup_repository(key)  # noqa: SLF001
        assert "enter a clone URL" in app.query_one(f"#repo-status-{key}", Static).render().plain

        clone_input = app.query_one("#repo-repository-0-clone-url", Input)
        name_input = app.query_one("#repo-repository-0-name", Input)
        clone_input.value = "https://github.com/company/application"
        name_input.value = "invalid"
        await app._setup_repository(key)  # noqa: SLF001
        assert "owner/name" in app.query_one(f"#repo-status-{key}", Static).render().plain

        name_input.value = "company/application"
        state["mode"] = "clone-failure"
        await app._setup_repository(key)  # noqa: SLF001
        assert "clone failed" in app.query_one(f"#repo-status-{key}", Static).render().plain

        state["mode"] = "graph-failure"
        await app._setup_repository(key)  # noqa: SLF001
        assert (
            "Graph generation failed" in app.query_one(f"#repo-status-{key}", Static).render().plain
        )

        app.query_one("#max-concurrent-tasks", Input).value = ""
        app.query_one("#save", Button).press()
        await pilot.pause()
        assert "cannot be blank" in app.query_one("#status", Static).render().plain
        app.query_one("#max-concurrent-tasks", Input).value = "not-a-number"
        app.query_one("#save", Button).press()
        await pilot.pause()
        assert "must be a number" in app.query_one("#status", Static).render().plain


def test_application_build_and_cli_dispatch(config: Config, tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    save_config(config, path)
    built = Application.build(path)
    assert built.storage.root == config.runtime_root.resolve()
    with patch("src.__main__.run_tui") as tui:
        main(["--config", str(path), "tui"])
        tui.assert_called_once_with(path)
    with (
        patch("src.__main__.Application.build", return_value=built),
        patch("src.__main__.create_server", return_value=Mock()) as create,
        patch("src.__main__.uvicorn.run") as run,
    ):
        main(["--config", str(path), "serve", "--no-worker"])
        create.assert_called_once_with(built, run_worker=False)
        run.assert_called_once()
    with (
        patch("src.__main__.Application.build", return_value=built),
        patch(
            "src.__main__.asyncio.run", side_effect=lambda coroutine: coroutine.close()
        ) as async_run,
    ):
        main(["--config", str(path), "worker"])
        main(["--config", str(path), "run", "incident.json"])
        assert async_run.call_count == 2


def test_application_loads_dotenv_without_overriding_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "from-process")
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=from-file\nGITHUB_WEBHOOK_SECRET=from-file\n",
        encoding="utf-8",
    )

    application = Application.build(tmp_path / ".agent" / "config.toml", agent_backend=AsyncMock())

    assert application.github.webhook_secret == "from-file"
    assert os.environ["OPENAI_API_KEY"] == "from-process"


def test_cli_tree_index_output_and_failures(tmp_path: Path, capsys) -> None:
    success = ToolResult(("tool",), 0, "stdout\n", "stderr\n")
    failure = ToolResult(("tool",), 7, "", "failed\n")
    with patch("src.__main__.capture_structured_tree", return_value=success):
        main(["tree", str(tmp_path), "--out", str(tmp_path / "tree.seed")])
    captured = capsys.readouterr()
    assert captured.out == "stdout\n" and captured.err == "stderr\n"
    with (
        patch("src.__main__.capture_structured_tree", return_value=failure),
        pytest.raises(SystemExit, match="7"),
    ):
        main(["tree", str(tmp_path)])

    with patch("src.__main__.build_repository_graphs", return_value=(success, success)):
        main(["index", str(tmp_path)])
    captured = capsys.readouterr()
    assert captured.out == "stdout\nstdout\n"
    assert captured.err == "failed\nstderr\nstderr\n"
    with (
        patch("src.__main__.build_repository_graphs", return_value=(success, failure)),
        pytest.raises(SystemExit, match="7"),
    ):
        main(["index", str(tmp_path)])


def test_tui_helpers_and_runner(tmp_path: Path) -> None:
    assert ConfigurationApp._field("Example", Button("Value"))  # noqa: SLF001
    with patch.object(ConfigurationApp, "run") as run:
        run_tui(tmp_path / "config.toml")
    run.assert_called_once_with()


@pytest.mark.asyncio
async def test_workflow_retry_block_and_error_paths(config: Config, incident: Incident) -> None:
    config.model.max_task_iterations = 1
    storage = Storage(config.runtime_root)
    github = FakeGitHub(config.github)
    verifier = FakeVerifier(config)

    class BlockedAgent(FakeAgent):
        async def implement_fix(self, task, worktree):  # noqa: ANN001, ANN201
            return FixResult(changed=False, summary="blocked", blocked_reason="guardrail")

    workflow = WorkflowEngine(
        config,
        storage,
        BlockedAgent(),  # type: ignore[arg-type]
        github,
        verifier,
    )
    task = storage.create_task(incident)
    (storage.root / "worktrees" / task.task_id).mkdir(parents=True)
    storage.transition(task.task_id, TaskState.REPRODUCING)
    assert (await workflow.process(task.task_id)).state == TaskState.BLOCKED

    failing_incident = incident.model_copy(update={"external_id": "INC-failing"})
    failing = storage.create_task(failing_incident)
    (storage.root / "worktrees" / failing.task_id).mkdir(parents=True)
    storage.transition(failing.task_id, TaskState.IMPLEMENTING)

    async def fail_test(_task, _worktree):  # noqa: ANN001, ANN202
        return False

    workflow.local_tester = fail_test
    failed = await workflow.process(failing.task_id)
    assert failed.state == TaskState.BLOCKED and "budget" in (failed.error or "")

    error_incident = incident.model_copy(update={"external_id": "INC-error"})
    errored = storage.create_task(error_incident)
    # No worktree and an unusable clone URL causes a caught processing failure.
    result = await workflow.process(errored.task_id)
    assert result.state == TaskState.FAILED and result.error
    assert await workflow.handle_github_event("ping", {}) is None


@pytest.mark.asyncio
async def test_workflow_requires_agent_reported_tests_to_pass(
    config: Config, incident: Incident
) -> None:
    config.model.max_task_iterations = 2
    storage = Storage(config.runtime_root)

    class FailedTestsAgent(FakeAgent):
        async def implement_fix(self, task, worktree):  # noqa: ANN001, ANN201
            return FixResult(changed=True, summary="tests failed", tests_passed=False)

        async def address_review(self, task, comments, worktree):  # noqa: ANN001, ANN201
            return ReviewResult(
                changed=True,
                summary="review tests failed",
                tests_passed=False,
                head_sha="new-sha",
            )

    workflow = WorkflowEngine(
        config,
        storage,
        FailedTestsAgent(),  # type: ignore[arg-type]
        FakeGitHub(config.github),
        FakeVerifier(config),
    )
    task = storage.create_task(incident)
    (storage.root / "worktrees" / task.task_id).mkdir(parents=True)
    storage.transition(task.task_id, TaskState.REPRODUCING)
    retry = await workflow.process(task.task_id)
    assert retry.state == TaskState.REPRODUCING and retry.attempts == 1
    blocked = await workflow.process(task.task_id)
    assert blocked.state == TaskState.BLOCKED
    assert "tests did not pass" in (blocked.error or "")

    review_incident = incident.model_copy(update={"external_id": "INC-review-tests"})
    review_task = storage.create_task(review_incident)
    (storage.root / "worktrees" / review_task.task_id).mkdir(parents=True)
    review_task = storage.transition(
        review_task.task_id,
        TaskState.WAITING_FOR_REVIEW,
        pr_number=42,
        pr_head_sha="old-sha",
    )
    comment = ReviewComment(
        id=1,
        body="please fix",
        author="owner",
        author_association="OWNER",
    )
    workflow._review_comments[review_task.task_id] = [comment]  # noqa: SLF001
    retry = await workflow.process(review_task.task_id)
    assert retry.state == TaskState.WAITING_FOR_REVIEW and retry.attempts == 1
    blocked = await workflow.process(review_task.task_id)
    assert blocked.state == TaskState.BLOCKED
    assert "review-change tests did not pass" in (blocked.error or "")


@pytest.mark.asyncio
async def test_failed_deployment_blocks_at_budget(config: Config, incident: Incident) -> None:
    config.model.max_task_iterations = 1
    storage = Storage(config.runtime_root)
    task = storage.create_task(incident)
    (storage.root / "worktrees" / task.task_id).mkdir(parents=True)
    task = storage.transition(
        task.task_id,
        TaskState.TESTING_DEPLOYMENT,
        pr_number=1,
        pr_head_sha="sha",
        deployment_environment="preview",
        deployment_sha="sha",
        deployment_url="https://preview.test",
    )

    class FailedVerifier(FakeVerifier):
        async def verify(self, task, deployment, worktree):  # noqa: ANN001, ANN201
            return VerificationResult(
                passed=False,
                environment="preview",
                sha="sha",
                url="https://preview.test",
                reason="browser failed",
            )

    workflow = WorkflowEngine(
        config,
        storage,
        FakeAgent(),  # type: ignore[arg-type]
        FakeGitHub(config.github),
        FailedVerifier(config),
    )
    blocked = await workflow.process(task.task_id)
    assert blocked.state == TaskState.BLOCKED
    assert blocked.playwright_status == "failed"


@pytest.mark.asyncio
async def test_cli_async_helpers(
    config: Config, incident: Incident, tmp_path: Path, capsys
) -> None:
    class Workflow:
        def __init__(self) -> None:
            self.task = None

        async def submit(self, submitted):  # noqa: ANN001, ANN202
            self.task = Storage(config.runtime_root).create_task(submitted)
            return self.task

        async def process(self, task_id):  # noqa: ANN001, ANN202
            self.task = Storage(config.runtime_root).transition(
                task_id, TaskState.WAITING_FOR_DEPLOYMENT
            )
            return self.task

        async def run_worker(self) -> None:
            return None

    connectors = Mock(start=AsyncMock(), stop=AsyncMock())
    application = Mock(connectors=connectors, workflow=Workflow())
    incident_path = tmp_path / "incident.json"
    incident_path.write_text(incident.model_dump_json())
    await _run_direct(application, incident_path)
    assert "waiting_for_pr_deployment" in capsys.readouterr().out
    await _worker(application)
    connectors.start.assert_awaited_once()
    connectors.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_verifier_unreachable_and_unknown_repository(
    config: Config, incident: Incident, tmp_path: Path
) -> None:
    storage = Storage(config.runtime_root)
    task = storage.create_task(incident)
    task.pr_number, task.pr_head_sha = 1, "sha"
    deployment = DeploymentReference(
        repository=task.repository,
        environment="preview",
        sha="sha",
        url="https://preview.test",
    )
    unknown = task.model_copy(update={"repository": "missing/repository"})
    assert not DeploymentVerifier(config).accepts(unknown, deployment)
    config.deployment.reachability_timeout_seconds = 0.001
    config.deployment.poll_interval_seconds = 0.001
    transport = httpx.MockTransport(lambda _: httpx.Response(503))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await DeploymentVerifier(config, client=client).verify(task, deployment, tmp_path)
    assert not result.passed and "reachable" in (result.reason or "")
    async_client = AsyncMock()
    async_client.get.side_effect = httpx.ConnectError("offline")
    with patch("src.verify.httpx.AsyncClient", return_value=async_client):
        assert not await DeploymentVerifier(config)._reachable(deployment.url)
    async_client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_remaining_workflow_retry_and_routing_paths(
    config: Config, incident: Incident
) -> None:
    config.model.max_task_iterations = 2
    storage = Storage(config.runtime_root)
    github = FakeGitHub(config.github)
    workflow = WorkflowEngine(
        config,
        storage,
        FakeAgent(),  # type: ignore[arg-type]
        github,
        FakeVerifier(config),
    )

    async def fail_test(_task, _worktree):  # noqa: ANN001, ANN202
        return False

    workflow.local_tester = fail_test
    task = storage.create_task(incident)
    (storage.root / "worktrees" / task.task_id).mkdir(parents=True)
    storage.transition(task.task_id, TaskState.IMPLEMENTING)
    assert (await workflow.process(task.task_id)).state == TaskState.REPRODUCING

    deployment_task = storage.create_task(incident.model_copy(update={"external_id": "INC-deploy"}))
    (storage.root / "worktrees" / deployment_task.task_id).mkdir(parents=True)
    storage.transition(
        deployment_task.task_id,
        TaskState.TESTING_DEPLOYMENT,
        pr_number=2,
        pr_head_sha="sha",
        deployment_environment="preview",
        deployment_sha="sha",
        deployment_url="https://preview.test",
    )

    class FailedVerifier(FakeVerifier):
        async def verify(self, task, deployment, worktree):  # noqa: ANN001, ANN201
            return VerificationResult(
                passed=False, environment="preview", sha="sha", url="https://preview.test"
            )

    workflow.verifier = FailedVerifier(config)
    assert (await workflow.process(deployment_task.task_id)).state == TaskState.REPRODUCING

    waiting = storage.create_task(incident.model_copy(update={"external_id": "INC-wait"}))
    (storage.root / "worktrees" / waiting.task_id).mkdir(parents=True)
    storage.transition(waiting.task_id, TaskState.WAITING_FOR_REVIEW)
    assert (await workflow.process(waiting.task_id)).state == TaskState.WAITING_FOR_REVIEW
    assert (
        await workflow.handle_github_event(
            "issue_comment",
            {
                "repository": {"full_name": incident.repository},
                "issue": {"number": 999},
                "comment": {},
            },
        )
        is None
    )

    # Exercise the polling loop's clean stop path.
    worker = WorkflowEngine(
        config,
        storage,
        FakeAgent(),  # type: ignore[arg-type]
        github,
        FakeVerifier(config),
    )
    worker.stop()
    await worker.run_worker()

    class NoChangeAgent(FakeAgent):
        async def implement_fix(self, task, worktree):  # noqa: ANN001, ANN201
            return FixResult(changed=False, summary="nothing to change")

    unchanged = storage.create_task(incident.model_copy(update={"external_id": "INC-unchanged"}))
    (storage.root / "worktrees" / unchanged.task_id).mkdir(parents=True)
    storage.transition(unchanged.task_id, TaskState.REPRODUCING)
    workflow.agent = NoChangeAgent()  # type: ignore[assignment]
    assert (await workflow.process(unchanged.task_id)).state == TaskState.BLOCKED

    retry = storage.create_task(incident.model_copy(update={"external_id": "INC-retry"}))
    # Invalid clone remains retryable until its configured iteration budget is exhausted.
    retried = await workflow.process(retry.task_id)
    assert retried.state == TaskState.RECEIVED and retried.attempts == 1


@pytest.mark.asyncio
async def test_worker_wakeups_are_deduplicated_and_replayed(
    config: Config, incident: Incident
) -> None:
    storage = Storage(config.runtime_root)
    workflow = WorkflowEngine(
        config,
        storage,
        FakeAgent(),  # type: ignore[arg-type]
        FakeGitHub(config.github),
        FakeVerifier(config),
    )
    task = storage.create_task(incident)
    await workflow.wake(task.task_id)
    await workflow.wake(task.task_id)
    assert workflow._wakeups.qsize() == 1  # noqa: SLF001
    assert await workflow._wakeups.get() == task.task_id  # noqa: SLF001
    workflow._queued_task_ids.discard(task.task_id)  # noqa: SLF001
    workflow._running_task_ids.add(task.task_id)  # noqa: SLF001
    await workflow.wake(task.task_id)
    assert workflow._wakeups.empty()  # noqa: SLF001
    assert task.task_id in workflow._deferred_wakeups  # noqa: SLF001


@pytest.mark.asyncio
async def test_workflow_indexes_latest_worktree_before_investigation(
    config: Config, incident: Incident, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "-b", "main", str(checkout)], check=True, capture_output=True)
    (checkout / "app.py").write_text("value = 1\n")
    subprocess.run(["git", "-C", str(checkout), "add", "app.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
    )
    config.repositories[0].clone_url = None
    config.repositories[0].local_path = checkout
    storage = Storage(config.runtime_root)
    calls: list[Path] = []
    success = ToolResult(("graph",), 0, "ok", "")

    def indexer(path: Path):
        calls.append(path)
        return success, success

    workflow = WorkflowEngine(
        config,
        storage,
        FakeAgent(),  # type: ignore[arg-type]
        FakeGitHub(config.github),
        FakeVerifier(config),
        repository_indexer=indexer,
    )
    task = storage.create_task(incident)
    processed = await workflow.process(task.task_id)
    assert processed.state == TaskState.COLLECTING_CONTEXT
    assert calls == [storage.root / "worktrees" / task.task_id]
    graph_events = [
        event for event in storage.events(task.task_id) if "graph_indexed" in event.type
    ]
    assert len(graph_events) == 2
    await workflow._worktree(processed)  # noqa: SLF001
    assert len(calls) == 1

    failure_incident = incident.model_copy(update={"external_id": "INC-graph-failure"})
    failed_task = storage.create_task(failure_incident)
    failure = ToolResult(("graphify",), 2, "", "broken graph")
    workflow.repository_indexer = lambda _path: (failure, success)
    failed = await workflow.process(failed_task.task_id)
    assert failed.state == TaskState.RECEIVED
    assert "graph generation failed" in (failed.error or "")


@pytest.mark.asyncio
async def test_worker_runs_queued_task_and_waits_for_shutdown(
    config: Config, incident: Incident
) -> None:
    storage = Storage(config.runtime_root)
    workflow = WorkflowEngine(
        config,
        storage,
        FakeAgent(),  # type: ignore[arg-type]
        FakeGitHub(config.github),
        FakeVerifier(config),
    )
    task = storage.create_task(incident)
    release = asyncio.Event()
    processed: list[str] = []

    async def process(task_id: str):  # noqa: ANN202
        processed.append(task_id)
        workflow.stop()
        asyncio.get_running_loop().call_later(0.02, release.set)
        await release.wait()
        return task.model_copy(update={"state": TaskState.COMPLETED})

    workflow.process = process  # type: ignore[method-assign]
    await asyncio.wait_for(workflow.run_worker(), timeout=1)
    assert processed == [task.task_id]
    assert not workflow._running_task_ids  # noqa: SLF001


@pytest.mark.asyncio
async def test_local_repository_branch_commit_and_local_pr(
    tmp_path: Path, incident: Incident
) -> None:
    incident = incident.model_copy(update={"external_id": "INC bad@{ref..lock"})
    runtime = tmp_path / ".agent"
    checkout = tmp_path / ".agents" / "repositories" / "company--application"
    checkout.mkdir(parents=True)
    subprocess.run(["git", "-C", str(checkout), "init", "-b", "main"], check=True)
    (checkout / "README.md").write_text("baseline\n")
    subprocess.run(["git", "-C", str(checkout), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-m",
            "initial",
        ],
        check=True,
    )
    config = Config(
        runtime_root=runtime,
        repositories=[],
    )
    storage = Storage(runtime)
    workflow = WorkflowEngine(
        config,
        storage,
        FakeAgent(),  # type: ignore[arg-type]
        GitHubService(config.github),
        FakeVerifier(config),
    )
    task = await workflow.submit(incident)
    worktree = storage.create_worktree(task, base_branch="main")
    assert task.branch and task.branch.startswith("agent/inc-bad-ref-lock-")
    (worktree / "FIX.md").write_text("local fix\n")
    (worktree / "graphify-out").mkdir()
    (worktree / "graphify-out" / "graph.json").write_text("{}")
    (worktree / ".code-review-graph").mkdir()
    (worktree / ".code-review-graph" / "graph.db").write_bytes(b"graph")
    storage.transition(task.task_id, TaskState.PUBLISHING_PR)
    completed = await workflow.process(task.task_id)
    assert completed.state == TaskState.COMPLETED
    assert completed.pr_url and completed.pr_url.startswith("local://")
    assert completed.pr_head_sha
    assert (storage.task_directory(task.task_id) / "pr.json").exists()
    assert (
        "FIX.md"
        in (storage.task_directory(task.task_id) / "artifacts/local/final.diff").read_text()
    )
    assert "committed locally" in (storage.task_directory(task.task_id) / "result.md").read_text()
    assert (
        subprocess.run(
            ["git", "-C", str(checkout), "show-ref", "--verify", f"refs/heads/{task.branch}"],
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )
    committed_files = subprocess.run(
        ["git", "-C", str(checkout), "ls-tree", "-r", "--name-only", task.branch or ""],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "FIX.md" in committed_files
    assert not any("graphify" in path or "code-review-graph" in path for path in committed_files)


def test_incident_worktree_pulls_latest_remote_before_branching(
    tmp_path: Path, incident: Incident
) -> None:
    remote = tmp_path / "remote.git"
    author = tmp_path / "author"
    managed = tmp_path / ".agent" / "repositories" / "company--application"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(author)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(author), "config", "user.name", "fixture"], check=True)
    subprocess.run(
        ["git", "-C", str(author), "config", "user.email", "fixture@example.test"],
        check=True,
    )
    (author / "version.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(author), "add", "version.txt"], check=True)
    subprocess.run(["git", "-C", str(author), "commit", "-m", "one"], check=True)
    subprocess.run(["git", "-C", str(author), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(author), "push", "-u", "origin", "main"], check=True)
    subprocess.run(["git", "clone", "--branch", "main", str(remote), str(managed)], check=True)

    storage = Storage(tmp_path / ".agent")
    (author / "version.txt").write_text("two\n")
    subprocess.run(["git", "-C", str(author), "commit", "-am", "two"], check=True)
    subprocess.run(["git", "-C", str(author), "push"], check=True)
    first = storage.create_task(incident)
    first_worktree = storage.create_worktree(first, base_branch="main", local_path=managed)
    assert (first_worktree / "version.txt").read_text() == "two\n"

    (author / "version.txt").write_text("three\n")
    subprocess.run(["git", "-C", str(author), "commit", "-am", "three"], check=True)
    subprocess.run(["git", "-C", str(author), "push"], check=True)
    (managed / "version.txt").write_text("local dirty change\n")
    second_incident = incident.model_copy(update={"external_id": "INC-1843"})
    second = storage.create_task(second_incident)
    second_worktree = storage.create_worktree(second, base_branch="main", local_path=managed)
    assert (second_worktree / "version.txt").read_text() == "three\n"

    mirror = tmp_path / "mirror.git"
    subprocess.run(["git", "clone", "--mirror", str(remote), str(mirror)], check=True)
    Storage._refresh_repository(mirror, "main")  # noqa: SLF001


def test_server_error_resources_and_signed_incident(
    config: Config, incident: Incident, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage(config.runtime_root)
    connectors = ConnectorManager(config.connectors)
    github = FakeGitHub(config.github, webhook_secret="secret")
    verifier = FakeVerifier(config)
    agent = IncidentAgent(config, storage, connectors)
    workflow = WorkflowEngine(config, storage, agent, github, verifier)
    application = Application(config, storage, connectors, github, agent, verifier, workflow)
    config.trigger.hook_path = "/custom/incidents"
    monkeypatch.setenv(config.server.webhook_secret_env, "intake-secret")
    body = incident.model_dump_json().encode()
    signature = "sha256=" + hmac.new(b"intake-secret", body, hashlib.sha256).hexdigest()
    with TestClient(create_server(application, run_worker=False)) as client:
        assert client.post("/hooks/incidents/sentry", content=body).status_code == 404
        assert client.post("/custom/incidents/sentry", content=body).status_code == 401
        assert (
            client.post(
                "/custom/incidents/sentry",
                content=body,
                headers={"x-agent-signature-256": signature},
            ).status_code
            == 202
        )
        assert client.get("/mcp/resources/tasks/missing/events").status_code == 404
        assert client.get("/mcp/resources/tasks/missing/result").status_code == 404
        assert client.post("/mcp/tools/cancel_task/missing").status_code == 404
        bad = incident.model_copy(update={"repository": "missing/repo"})
        response = client.post("/a2a/tasks", json={"incident": bad.model_dump(mode="json")})
        assert response.status_code == 422
