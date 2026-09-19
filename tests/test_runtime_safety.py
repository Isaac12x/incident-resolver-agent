from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from src.agent import IncidentAgent
from src.config import Config, ConnectorConfig
from src.connectors import ConnectorManager
from src.models import Incident
from src.storage import Storage
from src.tooling import repository_candidates
from src.tools import ToolError, WorkspaceTools


def _skill(root: Path, name: str) -> None:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        f"---\nname: {name}\ndescription: test\ntriggers:\n---\n# {name}\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_agent_run_manifest_fingerprints_prompt_skills_and_connectors(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _skill(skills, "code-review-graph")
    _skill(skills, "incident-investigation")
    _skill(skills, "show-me")
    _skill(skills, "code-review")
    storage = Storage(tmp_path / ".agent")
    task = storage.create_task(
        Incident(
            external_id="manifest-1",
            source="test",
            repository="owner/repo",
            environment="production",
            summary="manifest test",
        )
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    connector = ConnectorManager(
        [ConnectorConfig(name="logs", type="mcp", url="http://logs", capabilities=["logs"])]
    )
    seen: dict[str, object] = {}

    async def backend(instructions, prompt, tools, connector_tools, **kwargs):  # noqa: ANN001
        seen["prompt"] = prompt
        seen["instructions"] = instructions
        return {
            "root_cause": "known",
            "evidence": ["test"],
            "proposed_fix": "none",
            "reproducible": True,
        }

    agent = IncidentAgent(Config(), storage, connector, backend, skills_root=skills)
    await agent.investigate(task, worktree)
    manifest = next(
        event for event in storage.events(task.task_id) if event.type == "agent.run_manifest"
    )
    assert manifest.data["manifest_version"] == 1
    assert manifest.data["prompt_sha256"]
    assert manifest.data["system_prompt_sha256"]
    assert [item["name"] for item in manifest.data["skills"]] == [
        "code-review-graph",
        "incident-investigation",
        "show-me",
        "code-review",
    ]
    assert manifest.data["connectors_sha256"]
    from src.tooling import stable_hash

    assert manifest.data["skills"][-1]["sha256"] == hashlib.sha256(
        (skills / "code-review" / "SKILL.md").read_bytes()
    ).hexdigest()
    assert manifest.data["instructions_sha256"] == stable_hash(seen["instructions"])
    assert manifest.data["prompt_sha256"] == stable_hash(seen["prompt"])


@pytest.mark.asyncio
async def test_connector_discovery_retries_bounded_and_returns_on_recovery() -> None:
    manager = ConnectorManager([])
    calls = 0

    async def discover(_capabilities):
        nonlocal calls
        calls += 1
        if calls < 2:
            raise RuntimeError("transport down")
        return ["tool"]

    manager.tools_for = discover  # type: ignore[method-assign]
    assert await manager.discover_tools({"logs"}) == ["tool"]
    assert calls == 2

    manager.tools_for = discover  # type: ignore[method-assign]
    calls = 0
    async def always_down(_capabilities):
        nonlocal calls
        calls += 1
        raise RuntimeError("transport down")
    manager.tools_for = always_down  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="after 3 attempts"):
        await manager.discover_tools({"logs"})
    assert calls == 3


@pytest.mark.asyncio
async def test_connector_discovery_does_not_swallow_cancellation() -> None:
    manager = ConnectorManager([])

    async def cancelled(_capabilities):
        raise asyncio.CancelledError

    manager.tools_for = cancelled  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await manager.discover_tools(set())


def test_connector_manifest_descriptors_do_not_expose_endpoints() -> None:
    manager = ConnectorManager(
        [
            ConnectorConfig(
                name="logs",
                type="mcp",
                url="http://logs.example",
                capabilities=["logs"],
            )
        ]
    )
    descriptor = manager.descriptors()[0]
    assert set(descriptor) == {"name", "type", "transport", "capabilities", "endpoint_sha256"}
    assert "logs.example" not in str(descriptor)


def test_workspace_rejects_replaced_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    tools = WorkspaceTools(workspace)
    workspace.rename(tmp_path / "old-worktree")
    workspace.mkdir()
    with pytest.raises(ToolError, match="workspace changed"):
        tools.read_file("missing.txt")


def test_repository_candidates_deduplicates_case_aliases(tmp_path: Path) -> None:
    checkout = tmp_path / "Company--Application"
    checkout.mkdir()
    alias = tmp_path / "company--application"
    if not alias.exists():
        try:
            alias.symlink_to(checkout, target_is_directory=True)
        except OSError:
            pytest.skip("filesystem cannot create symlinks")
    assert alias.samefile(checkout)
    candidates = repository_candidates(tmp_path, "company/application")
    assert len([candidate for candidate in candidates if candidate.exists()]) == 1
