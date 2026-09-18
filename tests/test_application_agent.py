from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import Button, Input, TabbedContent

from src.agent import AgentRunContext, IncidentAgent, OpenAIAgentsBackend, SubscriptionCLIBackend
from src.config import (
    ApplicationConfig,
    CodeReviewConfig,
    Config,
    PermissionsConfig,
    RepositoryConfig,
    load_config,
    save_config,
)
from src.execution import container_command
from src.models import FixResult, Incident, SessionResult
from src.storage import Storage
from src.tools import ToolError, WorkspaceTools


@pytest.mark.asyncio
async def test_workspace_tools_select_each_repository_and_integration_root(tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    parent = tmp_path / "application"
    backend.mkdir()
    frontend.mkdir()
    parent.mkdir()
    (backend / "AGENTS.md").write_text("backend")
    (frontend / "AGENTS.md").write_text("frontend")

    tools = WorkspaceTools(
        {"api": backend, "web": frontend},
        parent_workspace=parent,
        default_repository="api",
    )
    assert tools.read_file("AGENTS.md", repository="web") == "frontend"
    tools.write_file("result.txt", "web result", repository="web")
    assert (frontend / "result.txt").read_text() == "web result"
    result = await tools.shell("pwd", repository="web")
    assert result.returncode == 0 and str(frontend) in result.stdout
    result = await tools.shell("pwd", integration=True)
    assert result.returncode == 0 and str(parent) in result.stdout
    with pytest.raises(ToolError, match="unknown repository"):
        tools.read_file("AGENTS.md", repository="missing")
    with pytest.raises(ToolError, match="escapes"):
        tools.read_file("../frontend/AGENTS.md", repository="api")
    replacement = tmp_path / "frontend-replacement"
    frontend.rename(replacement)
    frontend.mkdir()
    with pytest.raises(ToolError, match="changed"):
        await tools.shell("pwd", repository="web")


def test_application_container_mounts_member_controls_read_only(
    tmp_path: Path, monkeypatch
) -> None:
    parent = tmp_path / "application"
    backend = parent / "backend"
    frontend = parent / "frontend"
    backend.mkdir(parents=True)
    frontend.mkdir()
    (backend / ".git").mkdir()
    (frontend / ".agent").mkdir()
    (frontend / ".github").mkdir()
    monkeypatch.setattr("src.execution.shutil.which", lambda _: "/bin/docker")

    command = container_command(
        ["python", "check.py"],
        parent,
        Config().execution,
        Config().permissions,
        "application-test",
        protected_paths=[backend / ".git", frontend / ".agent", frontend / ".github"],
    )
    rendered = " ".join(command)
    assert "target=/workspace/backend/.git,readonly" in rendered
    assert "target=/workspace/frontend/.agent,readonly" in rendered
    assert "target=/workspace/frontend/.github,readonly" in rendered
    discovered = container_command(
        ["python", "check.py"],
        parent,
        Config().execution,
        Config().permissions,
        "application-discovery",
    )
    discovered_rendered = " ".join(discovered)
    assert "target=/workspace/backend/.git,readonly" in discovered_rendered
    assert "target=/workspace/frontend/.agent,readonly" in discovered_rendered
    allowed = container_command(
        ["python", "check.py"],
        parent,
        Config().execution,
        PermissionsConfig(allow_ci_modification=True),
        "application-ci-allowed",
    )
    assert "target=/workspace/frontend/.github,readonly" not in " ".join(allowed)


def test_application_configuration_round_trip(tmp_path: Path) -> None:
    config = Config(
        runtime_root=tmp_path / "runtime",
        repositories=[
            RepositoryConfig(name="shop/api", local_path=tmp_path),
            RepositoryConfig(name="shop/web", local_path=tmp_path),
        ],
        applications=[
            ApplicationConfig(
                name="storefront",
                services=["checkout-api", "checkout-web"],
                repositories=["shop/api", "shop/web"],
                integration_command="python integration.py",
            )
        ],
    )
    path = tmp_path / "config.toml"
    save_config(config, path)
    loaded = load_config(path)
    assert loaded.application("storefront").repositories == ["shop/api", "shop/web"]
    assert loaded.application("storefront").integration_command == "python integration.py"


@pytest.mark.asyncio
async def test_tui_application_membership_add_remove_and_save(tmp_path: Path) -> None:
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()
    path = tmp_path / "config.toml"
    save_config(
        Config(
            runtime_root=tmp_path / "runtime",
            repositories=[
                RepositoryConfig(name="shop/api", local_path=api),
                RepositoryConfig(name="shop/web", local_path=web),
            ],
            applications=[
                ApplicationConfig(
                    name="storefront",
                    services=["checkout"],
                    repositories=["shop/api", "shop/web"],
                    integration_command="python integration.py",
                )
            ],
        ),
        path,
    )
    from src.tui import ConfigurationApp

    app = ConfigurationApp(path)
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "applications-tab"
        await pilot.pause()
        key = app._application_keys[0]  # noqa: SLF001
        assert app.query_one(f"#application-{key}-name", Input).value == "storefront"
        assert (
            app.query_one(f"#application-{key}-repositories", Input).value
            == "shop/api, shop/web"
        )

        app.query_one("#add-application", Button).press()
        await pilot.pause()
        added = app._application_keys[-1]  # noqa: SLF001
        app.query_one(f"#remove-{added}", Button).press()
        await pilot.pause()
        app.query_one(f"#application-{key}-name", Input).value = "checkout"
        app.query_one("#save", Button).press()
        await pilot.pause()

    assert load_config(path).applications[0].name == "checkout"


@pytest.mark.asyncio
async def test_subscription_cli_application_runs_from_parent_and_skips_git_check(
    tmp_path: Path, monkeypatch
) -> None:
    parent = tmp_path / "application"
    repository = parent / "backend"
    web = parent / "frontend"
    parent.mkdir()
    repository.mkdir()
    web.mkdir()
    commands: list[tuple[str, ...]] = []
    real_spawn = asyncio.create_subprocess_exec

    class Process:
        returncode = 0

        def __init__(self) -> None:
            self.stdin = SimpleNamespace(
                write=lambda _: None, drain=_drain, close=lambda: None
            )
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdout.feed_data(
                b'{"type":"thread.started","thread_id":"application-thread"}\n'
                b'{"type":"item.completed","item":{"type":"agent_message",'
                b'"text":"{\\"summary\\":\\"ok\\"}"}}\n'
            )
            self.stdout.feed_eof()
            self.stderr.feed_eof()

        async def wait(self) -> int:
            return self.returncode

    async def _spawn(*command, **kwargs):  # noqa: ANN001
        commands.append(tuple(command))
        assert kwargs["cwd"] == parent
        return Process()

    async def _drain() -> None:
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    task = SimpleNamespace(  # only durable backend fields are needed here
        backend_session_id=None,
        repositories={"shop/api": object(), "shop/web": object()},
    )
    context = AgentRunContext(
        task=task,
        session_id="application-session",
        session_db=tmp_path / "session.sqlite3",
        lifecycle=SimpleNamespace(),
        save_backend_session=lambda _: None,
        memory_writer=lambda _: None,
    )
    backend = SubscriptionCLIBackend(Config())
    tools = WorkspaceTools(
        {"shop/api": repository, "shop/web": web}, parent_workspace=parent
    )
    result = await backend(
        "instructions",
        "prompt",
        tools,
        [],
        output_type=SessionResult,
        run_context=context,
    )
    assert result["summary"] == "ok"
    command = commands[0]
    assert "--skip-git-repo-check" in command
    assert command[command.index("--cd") + 1] == str(parent)
    task.backend_session_id = "application-thread"
    await backend(
        "instructions",
        "prompt",
        tools,
        [],
        output_type=SessionResult,
        run_context=context,
    )
    resumed = commands[1]
    assert resumed[resumed.index("resume") + 1] == "application-thread"
    assert "--skip-git-repo-check" in resumed
    monkeypatch.setattr(asyncio, "create_subprocess_exec", real_spawn)

    import code_review_graph.tools as graph_tools

    monkeypatch.setattr(graph_tools, "semantic_search_nodes", lambda **values: values)
    monkeypatch.setattr(graph_tools, "query_graph", lambda **values: values)
    monkeypatch.setattr(graph_tools, "get_impact_radius", lambda **values: values)
    async with backend._tool_bridge(context, tools) as launcher:  # noqa: SLF001
        async def bridge(name: str, arguments: dict[str, object]) -> dict[str, object]:
            completed = await asyncio.to_thread(
                subprocess.run,
                [str(launcher), name, json.dumps(arguments)],
                check=True,
                capture_output=True,
                text=True,
            )
            return json.loads(completed.stdout)

        assert (await bridge(
            "write_file", {"path": "bridge.txt", "content": "web", "repository": "shop/web"}
        ))["result"] == {"written": True}
        assert (await bridge("read_file", {"path": "bridge.txt", "repository": "shop/web"}))[
            "result"
        ] == {"content": "web"}
        assert (await bridge(
            "replace_in_file",
            {"path": "bridge.txt", "old": "web", "new": "updated", "repository": "shop/web"},
        ))["result"] == {"replaced": True}
        shell_result = await bridge("shell", {"command": "pwd", "repository": "shop/web"})
        assert shell_result.get("result"), shell_result
        assert shell_result["result"]["returncode"] == 0
        assert (await bridge(
            "code_graph_search", {"query": "total", "repository": "shop/web"}
        ))["result"]["repo_root"] == str(web)
        assert (await bridge(
            "code_graph_query",
            {"pattern": "callers_of", "target": "total", "repository": "shop/web"},
        ))["result"]["repo_root"] == str(web)
        assert (await bridge(
            "code_graph_impact", {"changed_files": ["x"], "repository": "shop/web"}
        ))["result"]["repo_root"] == str(web)


@pytest.mark.asyncio
async def test_sdk_tools_select_second_repository(tmp_path: Path, monkeypatch) -> None:
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()
    (api / "marker.txt").write_text("api")
    (web / "marker.txt").write_text("web")

    class Agent:
        def __init__(self, **values):  # noqa: ANN003
            self.__dict__.update(values)

    class Runner:
        @staticmethod
        async def run(agent, prompt, max_turns):  # noqa: ANN001, ANN202
            shell, read, write, replace = agent.tools[:4]
            assert read("marker.txt", repository="shop/web") == "web"
            assert write("selected.txt", "web", repository="shop/web") == "written"
            assert (
                replace("selected.txt", "web", "updated", repository="shop/web")
                == "replaced"
            )
            result = await shell("pwd", repository="shop/web")
            assert str(web) in result
            return types.SimpleNamespace(final_output={"changed": True, "summary": "selected"})

    fake_agents = types.SimpleNamespace(
        Agent=Agent,
        ModelSettings=lambda **values: types.SimpleNamespace(**values),
        Runner=Runner,
        function_tool=lambda function: function,
    )
    monkeypatch.setitem(sys.modules, "agents", fake_agents)
    backend = OpenAIAgentsBackend(Config())
    tools = WorkspaceTools({"shop/api": api, "shop/web": web})
    assert WorkspaceTools(
        {"shop/api": api, "shop/web": web}, default_repository="shop/web"
    ).read_file("marker.txt") == "web"
    result = await backend(
        "instructions",
        "prompt",
        tools,
        [],
        output_type=FixResult,
    )
    assert result["changed"] is True
    assert (web / "selected.txt").read_text() == "updated"
    assert not (api / "selected.txt").exists()


def test_application_instructions_keep_member_provenance_and_scope(tmp_path: Path) -> None:
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()
    (api / "AGENTS.md").write_text("API instructions")
    (web / "AGENTS.md").write_text("Web instructions")
    config = Config(
        runtime_root=tmp_path / "runtime",
        repositories=[
            RepositoryConfig(name="shop/api", local_path=api),
            RepositoryConfig(name="shop/web", local_path=web),
        ],
        applications=[ApplicationConfig(name="shop", repositories=["shop/api", "shop/web"])],
    )
    storage = Storage(config.runtime_root)
    task = storage.create_task(
        Incident(
            external_id="scope-1",
            source="test",
            application="shop",
            environment="production",
            summary="scope",
        ),
        config,
    )
    storage.append_memory("API memory", "shop/api")
    storage.append_memory("Web memory", "shop/web")
    agent = IncidentAgent(config, storage, SimpleNamespace())
    instructions = agent._instructions(  # noqa: SLF001
        task, {"shop/api": api, "shop/web": web}, ()
    )
    assert "# Repository instructions · shop/api" in instructions
    assert "# Repository instructions · shop/web" in instructions
    assert "API memory" in instructions and "Web memory" in instructions
    assert "Application Repository Scope" in instructions
    assert "configured application member repositories" in instructions


def test_application_instructions_include_member_ocr_reports(tmp_path: Path) -> None:
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()
    config = Config(
        runtime_root=tmp_path / "runtime",
        code_review=CodeReviewConfig(enabled=True, model="reviewer"),
        repositories=[
            RepositoryConfig(name="shop/api", local_path=api),
            RepositoryConfig(name="shop/web", local_path=web),
        ],
        applications=[ApplicationConfig(name="shop", repositories=["shop/api", "shop/web"])],
    )
    storage = Storage(config.runtime_root)
    task = storage.create_task(
        Incident(
            external_id="ocr-1",
            source="test",
            application="shop",
            environment="production",
            summary="ocr",
        ),
        config,
    )
    storage.write_artifact(
        task.task_id,
        "artifacts/code-review/shop--api/scan-result.json",
        '{"comments":[{"body":"API finding"}]}',
    )
    storage.write_artifact(
        task.task_id,
        "artifacts/code-review/shop--web/scan-result.json",
        '{"comments":[{"body":"Web finding"}]}',
    )
    agent = IncidentAgent(config, storage, SimpleNamespace())
    instructions = agent._instructions(  # noqa: SLF001
        task, {"shop/api": api, "shop/web": web}, ()
    )
    assert "OCR report · shop/api" in instructions
    assert "API finding" in instructions
    assert "OCR report · shop/web" in instructions
    assert "Web finding" in instructions


@pytest.mark.asyncio
async def test_application_agent_resolves_member_checkouts_from_parent_path(tmp_path: Path) -> None:
    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()
    config = Config(
        runtime_root=tmp_path / "runtime",
        repositories=[
            RepositoryConfig(name="shop/api", local_path=api),
            RepositoryConfig(name="shop/web", local_path=web),
        ],
        applications=[ApplicationConfig(name="shop", repositories=["shop/api", "shop/web"])],
    )
    storage = Storage(config.runtime_root)
    task = storage.create_task(
        Incident(
            external_id="parent-path",
            source="test",
            application="shop",
            environment="production",
            summary="resolve from parent",
        ),
        config,
    )
    parent = storage.repository_worktree(task)
    parent.mkdir(parents=True)
    for repository in task.repositories:
        storage.repository_worktree(task, repository).mkdir()

    class Connectors:
        async def discover_tools(self, _capabilities, retries=2):  # noqa: ARG002
            return []

    async def backend(_instructions, _prompt, tools, _connectors):  # noqa: ANN001
        assert tools.repository_names == ("shop/api", "shop/web")
        assert tools.session_workspace == parent
        return {"summary": "resolved"}

    agent = IncidentAgent(config, storage, Connectors(), backend)
    result = await agent._run(  # noqa: SLF001
        task,
        parent,
        "resolve",
        "resolve the incident",
        [],
        set(),
    )
    assert result == {"summary": "resolved"}
