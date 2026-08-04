from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent import IncidentAgent, OpenAIAgentsBackend
from src.config import Config, RepositoryConfig
from src.connectors import ConnectorManager
from src.models import Incident
from src.storage import Storage
from src.tools import ToolError, WorkspaceTools


def test_storage_searches_only_the_selected_conversation_with_rg(tmp_path: Path) -> None:
    storage = Storage(tmp_path / ".agent")
    storage.add_message("incident:repo:one", "assistant", "Root cause was a nullable session")
    storage.add_message("incident:repo:one", "user", "Which tests covered checkout?")
    storage.add_message("incident:repo:two", "assistant", "Root cause was unrelated")

    matches = storage.search_messages("incident:repo:one", "Root cause|checkout")

    assert [match["role"] for match in matches] == ["assistant", "user"]
    assert all("unrelated" not in str(match["content"]) for match in matches)
    assert storage.search_messages("incident:repo:one", "does-not-exist") == []
    assert storage.search_messages("missing", "anything") == []
    with pytest.raises(ValueError, match="1-500"):
        storage.search_messages("incident:repo:one", "")
    with pytest.raises(ValueError, match="between 1 and 50"):
        storage.search_messages("incident:repo:one", "root", 0)
    with pytest.raises(ValueError, match="invalid conversation search pattern"):
        storage.search_messages("incident:repo:one", "[")

    def unavailable(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise FileNotFoundError("rg")

    with pytest.raises(RuntimeError, match="could not start ripgrep"):
        storage.search_messages("incident:repo:one", "root", runner=unavailable)


def test_workspace_conversation_recall_is_bounded_and_optional(tmp_path: Path) -> None:
    calls: list[tuple[str, int]] = []
    tools = WorkspaceTools(
        tmp_path,
        conversation_searcher=lambda pattern, limit: calls.append((pattern, limit)) or [],
    )

    assert json.loads(tools.rg_conversation_history("root.*cause", 3)) == {"matches": []}
    assert calls == [("root.*cause", 3)]
    with pytest.raises(ToolError, match="not configured"):
        WorkspaceTools(tmp_path).rg_conversation_history("anything")


@pytest.mark.asyncio
async def test_agent_wires_scoped_history_search_and_recall_instructions(tmp_path: Path) -> None:
    config = Config(
        runtime_root=tmp_path / ".agent",
        repositories=[RepositoryConfig(name="company/application", local_path=tmp_path)],
    )
    storage = Storage(config.runtime_root)
    incident = Incident(
        external_id="INC-1",
        source="test",
        repository="company/application",
        environment="production",
        summary="Checkout fails",
    )
    task = storage.create_task(incident)
    storage.add_message(task.conversation_id, "assistant", "The null guard fixed checkout")
    worktree = storage.root / "worktrees" / task.task_id
    worktree.mkdir(parents=True)

    async def backend(instructions, prompt, tools, connector_tools):  # noqa: ANN001, ANN202
        assert "Durable Conversation Recall" in instructions
        assert "rg_conversation_history" in instructions
        matches = json.loads(tools.rg_conversation_history("null guard"))["matches"]
        assert matches[0]["content"] == "The null guard fixed checkout"
        return {"root_cause": "null session", "evidence": ["history"], "proposed_fix": "guard"}

    agent = IncidentAgent(config, storage, ConnectorManager([]), backend)
    assert (await agent.investigate(task, worktree)).root_cause == "null session"


@pytest.mark.asyncio
async def test_default_backend_exposes_history_as_a_model_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agents

    tools = WorkspaceTools(
        tmp_path,
        conversation_searcher=lambda pattern, limit: [
            {"role": "assistant", "content": f"{pattern}:{limit}"}
        ],
    )

    class SDKAgent:
        def __init__(self, **values):  # noqa: ANN003
            self.tools = values["tools"]

    class SDKRunner:
        @staticmethod
        async def run(agent, prompt, max_turns):  # noqa: ANN001, ANN202
            recall = next(
                tool for tool in agent.tools if tool.__name__ == "rg_conversation_history"
            )
            matches = json.loads(recall("why.*changed", 4))["matches"]
            assert matches[0]["content"] == "why.*changed:4"
            return SimpleNamespace(final_output='{"changed": true}')

    monkeypatch.setattr(agents, "Agent", SDKAgent)
    monkeypatch.setattr(agents, "Runner", SDKRunner)
    monkeypatch.setattr(agents, "function_tool", lambda function: function)

    result = await OpenAIAgentsBackend(Config())("instructions", "prompt", tools, [])
    assert result == {"changed": True}


def test_rg_failure_is_reported_without_shell_interpretation(tmp_path: Path) -> None:
    storage = Storage(tmp_path / ".agent")
    storage.add_message("incident", "assistant", "message")

    def failed(command, **_kwargs):  # noqa: ANN001, ANN202
        assert command[:2] == ["rg", "--json"]
        return subprocess.CompletedProcess(command, 2, "", "regex parse failed")

    with pytest.raises(ValueError, match="regex parse failed"):
        storage.search_messages("incident", "bad", runner=failed)
