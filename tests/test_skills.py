from __future__ import annotations

from pathlib import Path

import pytest

from src.agent import IncidentAgent
from src.config import AgentConfig, Config
from src.models import Incident
from src.skills import SkillResolver
from src.storage import Storage


def _write_skill(
    root: Path,
    folder: str,
    *,
    name: str,
    description: str = "",
    triggers: tuple[str, ...] = (),
) -> Path:
    path = root / folder / "SKILL.md"
    trigger_lines = "\n".join(f'  - "{trigger}"' for trigger in triggers)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f"name: {name}",
                f'description: "{description}"',
                "triggers:",
                trigger_lines,
                "---",
                "",
                f"# {name}",
                "",
                "Follow this skill.",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_skill_resolver_discovers_nested_skills_and_selects_context(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    repository = tmp_path / "repository"
    _write_skill(shared, "coding", name="coding", description="implement code")
    _write_skill(
        repository,
        "database-performance",
        name="database-performance",
        description="Diagnose slow database access",
        triggers=("slow database query", "query timeout"),
    )
    duplicate = _write_skill(repository, "coding", name="coding", description="duplicate")
    plain = repository / "security" / "SKILL.md"
    plain.parent.mkdir()
    plain.write_text("# Security\n\nInspect authentication boundaries.\n", encoding="utf-8")

    resolver = SkillResolver([shared, repository])
    resolution = resolver.resolve(
        ["coding", "missing-skill"],
        "Investigate a slow database query in checkout",
    )

    assert [skill.name for skill in resolution.discovered] == [
        "coding",
        "database-performance",
        "security",
    ]
    assert [skill.name for skill in resolution.selected] == ["coding", "database-performance"]
    assert all(skill.content.startswith("---") for skill in resolution.selected)
    assert resolution.selected[0].path != duplicate
    assert resolution.missing_required == ("missing-skill",)


def test_skill_resolver_honors_auto_load_limit_and_ignores_missing_roots(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "security", name="security", triggers=("security incident",))
    _write_skill(root, "authentication", name="authentication", triggers=("authentication",))
    resolver = SkillResolver([tmp_path / "missing", root], max_auto_skills=1)

    resolution = resolver.resolve([], "authentication security incident")

    assert len(resolution.selected) == 1
    assert resolution.selected[0].name == "security"


def test_agent_skill_directories_must_be_repository_relative() -> None:
    assert AgentConfig(skill_directories=["skills", ".agents/skills"]).max_auto_skills == 8
    with pytest.raises(ValueError, match="repository-relative"):
        AgentConfig(skill_directories=["../shared-skills"])
    with pytest.raises(ValueError, match="repository-relative"):
        AgentConfig(skill_directories=["/tmp/skills"])
    with pytest.raises(ValueError, match="repository-relative"):
        AgentConfig(skill_directories=[""])


@pytest.mark.asyncio
async def test_agent_fails_closed_when_a_required_skill_is_missing(tmp_path: Path) -> None:
    config = Config(runtime_root=tmp_path / ".agent")
    storage = Storage(config.runtime_root)
    task = storage.create_task(
        Incident(
            external_id="INC-SKILLS",
            source="test",
            repository="company/application",
            environment="production",
            summary="Missing skill",
        )
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    async def backend(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("backend must not run without required skills")

    agent = IncidentAgent(
        config,
        storage,
        object(),
        backend,
        skills_root=tmp_path / "empty-skills",
    )

    with pytest.raises(RuntimeError, match="code-review-graph, incident-investigation"):
        await agent.investigate(task, worktree)
    event = storage.events(task.task_id)[-1]
    assert event.type == "agent.skills_resolved"
    assert event.data["missing_required"] == ["code-review-graph", "incident-investigation"]
