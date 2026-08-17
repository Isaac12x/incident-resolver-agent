from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agent import (
    AgentRunContext,
    IncidentAgent,
    OpenAIAgentsBackend,
    SubscriptionCLIBackend,
    _CompactingSession,
)
from src.config import Config, ConnectorConfig, ModelConfig, RepositoryConfig
from src.connectors import ConnectorManager
from src.github import GitHubService
from src.models import (
    FixResult,
    Incident,
    InvestigationResult,
    PullRequestReference,
    ReviewComment,
    ReviewResult,
    SessionResult,
    TaskState,
    VerificationResult,
)
from src.storage import Storage
from src.tools import WorkspaceTools
from src.verify import DeploymentVerifier
from src.workflow import WorkflowEngine, _TaskLifecycle


class _GitHub(GitHubService):
    def __init__(self, config):  # noqa: ANN001
        super().__init__(config)
        self.updated: list[str] = []

    async def create_pull_request(self, task):  # noqa: ANN001, ANN201
        return PullRequestReference(
            repository=task.repository,
            number=17,
            url="https://github.test/pull/17",
            head_sha="initial-sha",
            branch=task.branch or "agent/fix",
        )

    async def publish_verification(self, task, result):  # noqa: ANN001, ANN201
        return None

    async def update_pull_request(self, task):  # noqa: ANN001, ANN201
        self.updated.append(task.pr_head_sha or "")
        return PullRequestReference(
            repository=task.repository,
            number=task.pr_number or 17,
            url=task.pr_url or "https://github.test/pull/17",
            head_sha=task.pr_head_sha or "",
            branch=task.branch or "agent/fix",
        )


class _Verifier(DeploymentVerifier):
    async def verify(self, task, deployment, worktree):  # noqa: ANN001, ANN201
        return VerificationResult(
            passed=True,
            environment=deployment.environment,
            sha=deployment.sha,
            url=deployment.url,
        )


@pytest.mark.asyncio
async def test_real_agent_path_uses_lifecycle_tools_in_one_resumable_task_session(
    tmp_path: Path,
) -> None:
    config = Config(runtime_root=tmp_path / ".agent")
    config.repositories.append(
        RepositoryConfig(
            name="company/service",
            publish_mode="github",
            clone_url="https://example.invalid/service.git",
        )
    )
    storage = Storage(config.runtime_root)
    incident = Incident(
        external_id="INC-9",
        source="test",
        repository="company/service",
        environment="production",
        summary="checkout fails",
    )
    task = storage.create_task(incident)
    worktree = storage.root / "worktrees" / task.task_id
    worktree.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    (worktree / "base.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(worktree), "add", "base.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
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
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "-C", str(worktree), "remote", "add", "origin", str(remote)], check=True)
    calls: list[AgentRunContext] = []

    class Backend(OpenAIAgentsBackend):
        async def __call__(
            self,
            instructions,
            prompt,
            tools,
            connector_tools,
            output_type=None,
            run_context=None,
        ):  # noqa: ANN001, ANN202
            del instructions, prompt, connector_tools
            assert output_type is SessionResult and run_context is not None
            calls.append(run_context)
            if len(calls) == 1:
                await run_context.lifecycle.mark_investigation_complete(
                    "missing guard", ["trace"], "add a guard", True
                )
                tools.write_file("fix.py", "guard = True\n")
            else:
                tools.write_file("review.txt", "addressed\n")
            tested = await run_context.lifecycle.run_tests("python --version")
            assert tested["passed"]
            published = await run_context.lifecycle.open_pr("guard added and verified")
            assert published["state"] == TaskState.WAITING_FOR_DEPLOYMENT
            return {
                "summary": "yielding for deployment",
                "waiting_for_external_event": True,
            }

    agent = IncidentAgent(config, storage, ConnectorManager([]), Backend(config))
    github = _GitHub(config.github)
    workflow = WorkflowEngine(config, storage, agent, github, _Verifier(config))

    assert (await workflow.process(task.task_id)).state == TaskState.COLLECTING_CONTEXT
    task = await workflow.process(task.task_id)
    assert task.state == TaskState.WAITING_FOR_DEPLOYMENT
    assert len(calls) == 1
    assert calls[0].session_id == task.agent_session_id
    assert storage.read_task_memory(task.task_id).count("Investigation") == 1
    lifecycle_events = [event.type for event in storage.events(task.task_id)]
    assert "task.investigating" in lifecycle_events
    assert "verification.local" in lifecycle_events
    assert "task.publishing_pr" in lifecycle_events

    task = storage.transition(task.task_id, TaskState.WAITING_FOR_REVIEW)
    workflow._review_comments[task.task_id] = [
        ReviewComment(id=91, author="owner", body="add a note")
    ]  # noqa: SLF001
    task = await workflow.process(task.task_id)
    assert task.state == TaskState.WAITING_FOR_DEPLOYMENT
    assert len(calls) == 2
    assert calls[0].session_id == calls[1].session_id
    assert len(github.updated) == 1
    assert task.pr_head_sha == subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.asyncio
async def test_durable_route_keeps_deployment_verification_and_review_recovery(
    tmp_path: Path,
) -> None:
    config = Config(runtime_root=tmp_path / ".agent")
    config.repositories.append(RepositoryConfig(name="company/service", publish_mode="github"))
    storage = Storage(config.runtime_root)
    incident = Incident(
        external_id="INC-DEPLOY",
        source="test",
        repository="company/service",
        environment="production",
        summary="deployment route",
    )
    task = storage.create_task(incident)
    worktree = storage.root / "worktrees" / task.task_id
    worktree.mkdir(parents=True)
    task.pr_number = 17
    task.pr_head_sha = "sha-1"
    task.branch = "agent/fix"
    storage.save_task(task)
    storage.transition(task.task_id, TaskState.WAITING_FOR_DEPLOYMENT)

    class NoAgentBackend(OpenAIAgentsBackend):
        async def __call__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            raise AssertionError("deployment verification must not invoke the agent")

    class TrackingVerifier(_Verifier):
        def __init__(self, config):  # noqa: ANN001
            super().__init__(config)
            self.calls = 0

        async def verify(self, task, deployment, worktree):  # noqa: ANN001, ANN201
            self.calls += 1
            return await super().verify(task, deployment, worktree)

    verifier = TrackingVerifier(config)
    github = _GitHub(config.github)
    agent = IncidentAgent(config, storage, ConnectorManager([]), NoAgentBackend(config))
    workflow = WorkflowEngine(config, storage, agent, github, verifier)
    deployment = await workflow.handle_github_event(
        "deployment_status",
        {
            "repository": {"full_name": "company/service"},
            "pull_request": {"number": 17},
            "deployment": {"id": 4, "environment": "preview", "sha": "sha-1"},
            "deployment_status": {
                "state": "success",
                "environment_url": "https://preview.example.test",
            },
        },
    )
    assert deployment and deployment.state == TaskState.TESTING_DEPLOYMENT
    verified = await workflow.process(task.task_id)
    assert verified.state == TaskState.WAITING_FOR_REVIEW
    assert verifier.calls == 1

    review_payload = {
        "action": "created",
        "repository": {"full_name": "company/service"},
        "pull_request": {"number": 17},
        "comment": {
            "id": 44,
            "body": "Please add a regression test",
            "user": {"login": "owner"},
            "author_association": "OWNER",
        },
    }
    await workflow.handle_github_event("pull_request_review_comment", review_payload)
    persisted = storage.load_task(task.task_id)
    assert [comment.id for comment in persisted.pending_review_comments] == [44]
    recovered = WorkflowEngine(config, storage, agent, github, verifier)
    await recovered.recover()
    assert await recovered._wakeups.get() == task.task_id


@pytest.mark.asyncio
async def test_lifecycle_tools_fail_closed_and_support_repository_memory(tmp_path: Path) -> None:
    config = Config(runtime_root=tmp_path / ".agent")
    config.model.max_task_iterations = 1
    config.repositories.append(RepositoryConfig(name="company/service", publish_mode="github"))
    storage = Storage(config.runtime_root)
    task = storage.create_task(
        Incident(
            external_id="INC-GUARDS",
            source="test",
            repository="company/service",
            environment="production",
            summary="guard paths",
        )
    )
    worktree = storage.root / "worktrees" / task.task_id
    worktree.mkdir(parents=True)
    workflow = WorkflowEngine(
        config,
        storage,
        SimpleNamespace(),
        _GitHub(config.github),
        _Verifier(config),
        reproducer=AsyncMock(return_value=True),
        local_tester=AsyncMock(return_value=False),
    )
    lifecycle = _TaskLifecycle(workflow, task.task_id, worktree)
    with pytest.raises(ValueError, match="required"):
        await lifecycle.mark_investigation_complete("", [], "fix")
    with pytest.raises(RuntimeError, match="cannot complete"):
        await lifecycle.mark_investigation_complete("cause", [], "fix")
    with pytest.raises(RuntimeError, match="successful local verification"):
        await lifecycle.open_pr("too early")
    with pytest.raises(ValueError, match="blank"):
        await lifecycle.remember("  ")
    with pytest.raises(ValueError, match="scope"):
        await lifecycle.remember("note", "global")

    storage.transition(task.task_id, TaskState.COLLECTING_CONTEXT)
    investigated = await lifecycle.mark_investigation_complete("cause", ["trace"], "fix")
    assert investigated == {"state": "reproducing", "reproduced": True}
    assert await lifecycle.remember("repository fact", "repository") == {
        "stored": True,
        "scope": "repository",
    }
    assert "repository fact" in storage.read_memory(task.repository)
    failed = await lifecycle.run_tests("python --version")
    assert not failed["passed"]
    assert failed["state"] == TaskState.BLOCKED


@pytest.mark.asyncio
async def test_session_compaction_writes_memory_and_keeps_recent_items() -> None:
    class Session:
        session_id = "task:1"
        session_settings = None

        def __init__(self) -> None:
            self.items: list[dict[str, str]] = []

        async def get_items(self, limit=None):  # noqa: ANN001, ANN201
            return self.items[-limit:] if limit else list(self.items)

        async def add_items(self, items):  # noqa: ANN001, ANN201
            self.items.extend(items)

        async def pop_item(self):  # noqa: ANN201
            return self.items.pop() if self.items else None

        async def clear_session(self):  # noqa: ANN201
            self.items.clear()

    memory: list[str] = []
    underlying = Session()
    session = _CompactingSession(underlying, threshold=3, keep=2, memory_writer=memory.append)
    await session.add_items(
        [{"role": "user", "content": f"message {index}"} for index in range(4)]
    )
    assert memory and "message 1" in memory[0]
    assert len(await session.get_items()) == 3
    assert "Durable checkpoint" in (await session.get_items())[0]["content"]
    assert await session.pop_item() == {"role": "user", "content": "message 3"}
    await session.clear_session()
    assert not await session.get_items()


@pytest.mark.asyncio
async def test_agents_sdk_attaches_durable_main_and_subagent_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config(runtime_root=tmp_path / ".agent")
    config.model.show_execution_details = False
    config.model.compaction_enabled = False
    created_sessions: list[str] = []
    created_agents: list[SimpleNamespace] = []

    class SQLiteSession:
        def __init__(self, session_id, db_path):  # noqa: ANN001
            self.session_id = session_id
            self.db_path = db_path
            created_sessions.append(session_id)

    class Agent(SimpleNamespace):
        def __init__(self, **values):  # noqa: ANN003
            super().__init__(**values)
            created_agents.append(self)

        def as_tool(self, name, description, **values):  # noqa: ANN001, ANN003, ANN201
            return {"name": name, "description": description, **values}

    class Runner:
        @staticmethod
        async def run(agent, prompt, max_turns, session):  # noqa: ANN001, ANN202
            assert prompt == "continue"
            assert max_turns == config.model.max_turns_per_iteration
            assert session.session_id == "task:durable"
            assert {tool["name"] for tool in agent.tools if isinstance(tool, dict)} == {
                "delegate_research",
                "delegate_implementation",
            }
            lifecycle_names = {
                tool.__name__ for tool in agent.tools if callable(tool)
            }
            assert {
                "mark_investigation_complete",
                "run_tests",
                "open_pr",
                "remember",
            }.issubset(lifecycle_names)
            return SimpleNamespace(
                final_output={"summary": "checkpoint", "waiting_for_external_event": True}
            )

    fake_agents = SimpleNamespace(
        Agent=Agent,
        ModelSettings=lambda **values: SimpleNamespace(**values),
        Runner=Runner,
        SQLiteSession=SQLiteSession,
        function_tool=lambda function: function,
    )
    monkeypatch.setitem(sys.modules, "agents", fake_agents)
    lifecycle = SimpleNamespace(
        mark_investigation_complete=AsyncMock(),
        run_tests=AsyncMock(),
        open_pr=AsyncMock(),
        remember=AsyncMock(),
    )
    task = SimpleNamespace(backend_session_id=None)
    context = AgentRunContext(
        task=task,
        session_id="task:durable",
        session_db=tmp_path / "sessions.sqlite3",
        lifecycle=lifecycle,
        save_backend_session=lambda value: None,
        memory_writer=lambda value: None,
    )
    result = await OpenAIAgentsBackend(config)(
        "instructions",
        "continue",
        WorkspaceTools(tmp_path),
        [],
        output_type=SessionResult,
        run_context=context,
    )
    assert result["summary"] == "checkpoint"
    assert created_sessions == [
        "task:durable",
        "task:durable:research",
        "task:durable:implementation",
    ]
    assert [agent.name for agent in created_agents] == [
        "Incident Researcher",
        "Incident Implementer",
        "Incident Resolver",
    ]


@pytest.mark.asyncio
async def test_subscription_cli_maps_mcp_bridges_tools_parses_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config(
        runtime_root=tmp_path / ".agent",
        model=ModelConfig(runtime="subscription-cli", subscription_command=["codex"]),
        connectors=[
            ConnectorConfig(
                name="logs",
                transport="stdio",
                command=["log-mcp", "serve"],
                capabilities=["logs"],
            )
        ],
    )
    storage = Storage(config.runtime_root)
    task = storage.create_task(
        Incident(
            external_id="INC-CLI",
            source="test",
            repository="company/service",
            environment="production",
            summary="CLI incident",
        )
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    remembered: list[tuple[str, str]] = []
    lifecycle = SimpleNamespace(
        remember=AsyncMock(
            side_effect=lambda note, scope="task": remembered.append((note, scope))
            or {"stored": True}
        )
    )
    for name in ("mark_investigation_complete", "run_tests", "open_pr"):
        setattr(lifecycle, name, AsyncMock(return_value={"state": "ok"}))
    saved: list[str] = []
    real_subprocess = asyncio.create_subprocess_exec
    commands: list[list[str]] = []
    structured_outputs = [
        {"summary": "CLI checkpoint", "waiting_for_external_event": True},
        {
            "root_cause": "bad cache key",
            "evidence": ["trace"],
            "proposed_fix": "scope the key",
            "reproducible": True,
        },
        {"changed": True, "summary": "fixed", "tests_passed": True},
        {"changed": False, "summary": "reviewed", "tests_passed": True},
    ]

    class RuntimeConnector:
        name = "runtime-logs"

        async def list_tools(self):  # noqa: ANN201
            return [SimpleNamespace(name="search")]

        async def call_tool(self, name, arguments):  # noqa: ANN001, ANN201
            assert name == "search" and arguments == {"query": "checkout"}
            return {"matches": 2}

    class Process:
        returncode = 0

        def __init__(self, command: list[str]) -> None:
            self.command = command

        async def communicate(self, prompt: bytes):
            assert b"incident-session-tool" in prompt
            child = await real_subprocess(
                str(worktree / "graphify-out" / "incident-session-tool"),
                "remember",
                json.dumps({"note": "CLI memory", "scope": "task"}),
                stdout=asyncio.subprocess.PIPE,
            )
            stdout, _ = await child.communicate()
            assert json.loads(stdout)["ok"]
            child = await real_subprocess(
                str(worktree / "graphify-out" / "incident-session-tool"),
                "write_file",
                json.dumps({"path": "cli-change.txt", "content": "mapped write\n"}),
                stdout=asyncio.subprocess.PIPE,
            )
            stdout, _ = await child.communicate()
            assert json.loads(stdout)["result"] == {"written": True}
            child = await real_subprocess(
                str(worktree / "graphify-out" / "incident-session-tool"),
                "connector_call",
                json.dumps(
                    {
                        "connector": "runtime-logs",
                        "tool": "search",
                        "arguments": {"query": "checkout"},
                    }
                ),
                stdout=asyncio.subprocess.PIPE,
            )
            stdout, _ = await child.communicate()
            assert json.loads(stdout)["result"] == {"matches": 2}
            output = Path(self.command[self.command.index("--output-last-message") + 1])
            output.write_text(json.dumps(structured_outputs.pop(0)))
            event = json.dumps({"type": "thread.started", "thread_id": "thread-123"})
            return event.encode(), b""

    async def create_subprocess(*command, **kwargs):  # noqa: ANN003, ANN202
        assert kwargs["cwd"] == worktree
        commands.append(list(command))
        return Process(list(command))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)

    def context(current_task):  # noqa: ANN001, ANN202
        return AgentRunContext(
            task=current_task,
            session_id=current_task.agent_session_id or "missing",
            session_db=storage.root / "sessions.sqlite3",
            lifecycle=lifecycle,
            save_backend_session=saved.append,
            memory_writer=lambda value: storage.append_task_memory(task.task_id, value),
            capabilities=frozenset({"logs"}),
            connector_tools=(RuntimeConnector(),),
        )

    backend = SubscriptionCLIBackend(config)
    result = await backend(
        "instructions",
        "prompt",
        WorkspaceTools(worktree),
        [],
        output_type=SessionResult,
        run_context=context(task),
    )
    assert result["summary"] == "CLI checkpoint"
    assert saved == ["thread-123"]
    assert remembered == [("CLI memory", "task")]
    assert commands[0][:3] == ["codex", "--yolo", "exec"]
    assert "mcp_servers.logs.command=\"log-mcp\"" in commands[0]
    assert commands[0][commands[0].index("--sandbox") + 1] == "read-only"
    assert (worktree / "cli-change.txt").read_text() == "mapped write\n"

    resumed = task.model_copy(update={"backend_session_id": "thread-123"})
    parsed = []
    for output_type in (InvestigationResult, FixResult, ReviewResult):
        parsed.append(
            await backend(
                "instructions",
                "resume",
                WorkspaceTools(worktree),
                [],
                output_type=output_type,
                run_context=context(resumed),
            )
        )
    assert commands[1][3:5] == ["resume", "thread-123"]
    assert parsed[0]["root_cause"] == "bad cache key"
    assert parsed[1]["changed"] and parsed[1]["tests_passed"]
    assert parsed[2]["summary"] == "reviewed"
