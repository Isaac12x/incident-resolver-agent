from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.app import Application
from src.config import (
    Config,
    ModelConfig,
    RepositoryConfig,
    load_config,
    save_config,
    upgrade_model,
)
from src.models import Incident, TaskState
from src.verification_graph import VerificationGraph
from src.workflow import _TaskLifecycle


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "area").mkdir()
    for name in ("seed", "caller", "outer", "sibling"):
        (root / "area" / f"{name}.py").write_text("value = 1\n")
    (root / "outside.py").write_text("value = 1\n")
    (root / "settings.toml").write_text("enabled = true\n")
    (root / ".gitignore").write_text("__pycache__/\n.code-review-graph/\n")
    refresh_graph(root)
    return root


def refresh_graph(root):
    graph = root / ".code-review-graph/graph.db"
    graph.parent.mkdir(exist_ok=True)
    with sqlite3.connect(graph) as db:
        db.executescript("""
            DROP TABLE IF EXISTS nodes;
            DROP TABLE IF EXISTS edges;
            CREATE TABLE nodes(kind, name, qualified_name, file_path, file_hash);
            CREATE TABLE edges(kind, source_qualified, target_qualified, file_path);
        """)
        for path in root.rglob("*.py"):
            db.execute(
                "INSERT INTO nodes VALUES (?, ?, ?, ?, ?)",
                (
                    "Test" if path.stem.startswith("test_") else "Function",
                    path.stem,
                    str(path) + "::function",
                    str(path),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                ),
            )
        for source, target in (("caller", "seed"), ("outer", "caller")):
            db.execute(
                "INSERT INTO edges VALUES (?, ?, ?, ?)",
                (
                    "CALLS",
                    str(root / f"area/{source}.py") + "::function",
                    str(root / f"area/{target}.py") + "::function",
                    str(root / f"area/{source}.py"),
                ),
            )
        db.execute(
            "INSERT INTO edges VALUES (?, ?, ?, ?)",
            (
                "CALLS",
                str(root / "outside.py") + "::function",
                "external::missing",
                str(root / "outside.py"),
            ),
        )


def ledger(root):
    return VerificationGraph(root, root.parent / "verification.json")


def passed(graph, paths, command="test"):
    graph.record(command, paths, graph.snapshot(paths), {"passed": True, "returncode": 0})


def test_rings_resume_and_hash_invalidation(repository):
    graph = ledger(repository)
    plan = graph.plan(["area/seed.py"], ["area"])
    assert plan["phase"] == "fix_incident"
    assert plan["rings"] == [
        [f"area/{name}.py"]
        for name in (
            "seed",
            "caller",
            "outer",
            "sibling",
        )
    ]
    passed(graph, ["area/seed.py"])
    assert ledger(repository).plan([], ["area"])["next_paths"] == ["area/caller.py"]
    for name in ("caller", "outer", "sibling"):
        passed(graph, [f"area/{name}.py"])
    assert graph.plan([], ["area"])["phase"] == "complete"
    (repository / "area/seed.py").write_text("value = 2\n")
    refresh_graph(repository)
    assert graph.plan([], ["area"])["next_ring"] == 0
    assert graph.pending_checks()
    # The disconnected sibling still has current evidence after the graph refresh.
    assert graph.cached("test", ["area/sibling.py"], graph.snapshot(["area/sibling.py"]))


def test_cache_dependencies_config_failures_and_graph_fallback(repository):
    graph = ledger(repository)
    paths = ["area/seed.py"]
    passed(graph, paths)
    inputs = graph.snapshot(paths)
    assert "area/caller.py" in inputs and "outside.py" not in inputs
    assert graph.cached("test", paths, inputs)
    assert not graph.cached("different command", paths, inputs)
    (repository / "outside.py").write_text("value = 2\n")
    # Stale graph is deliberately conservative.
    assert not graph.cached("test", paths, graph.snapshot(paths))
    refresh_graph(repository)
    assert graph.cached("test", paths, graph.snapshot(paths))
    (repository / "settings.toml").write_text("enabled = false\n")
    assert not graph.cached("test", paths, graph.snapshot(paths))
    passed(graph, paths)
    graph.record("test", paths, graph.snapshot(paths), {"passed": False})
    assert not graph.cached("test", paths, graph.snapshot(paths))
    assert graph.pending_checks() == ["test"]
    with pytest.raises(ValueError, match="repository files"):
        graph.snapshot(["missing.py"])
    (repository / ".code-review-graph/graph.db").unlink()
    assert graph.snapshot(paths) == graph.files()
    passed(graph, paths)
    assert graph.data["nodes"][0]["kind"] == "File"
    assert not graph.pending_checks()


def test_test_runner_inputs_are_hashed_without_claiming_coverage(repository):
    graph = ledger(repository)
    paths = ["area/seed.py"]
    command = "python outside.py"
    inputs = graph.snapshot(paths, command)
    assert "outside.py" in inputs
    graph.record(command, paths, inputs, {"passed": True})
    assert not graph.pending_checks()
    (repository / "outside.py").write_text("value = 2\n")
    refresh_graph(repository)
    assert graph.pending_checks() == [command]
    assert not graph.cached(command, paths, graph.snapshot(paths, command))


def test_resume_after_committed_deletion_and_rename(repository):
    graph = ledger(repository)
    graph.plan(["area/seed.py"], ["area"])
    passed(graph, ["area/seed.py"])
    (repository / "area/seed.py").rename(repository / "area/replacement.py")
    refresh_graph(repository)
    assert graph.plan([], ["area"])["next_ring"] == 0
    assert graph.pending_checks() == ["test"]
    assert graph.snapshot(["area/seed.py"])["area/seed.py"] == "deleted"
    plan = graph.plan(["area/seed.py", "area/replacement.py"], ["area"])
    assert plan["next_paths"] == ["area/replacement.py", "area/seed.py"]


def test_boundaries_unscoped_checks_new_and_deleted_files(repository):
    graph = ledger(repository)
    for paths in (["outside.py"], ["../escape"]):
        with pytest.raises(ValueError):
            graph.plan(paths, ["area"])
    with pytest.raises(ValueError, match="control"):
        graph.relative(".git/config")
    with pytest.raises(ValueError):
        RepositoryConfig(name="repo", responsibility_paths=["../escape"])
    graph.plan(["area/seed.py"], ["area"])
    with pytest.raises(ValueError, match="fixed"):
        graph.plan(["area/caller.py"], ["area"])
    passed(graph, None)
    assert graph.plan([], ["area"])["next_ring"] == 0
    subprocess.run(["git", "add", "area/seed.py"], cwd=repository, check=True)
    (repository / "area/seed.py").unlink()
    assert graph.files()["area/seed.py"] == "deleted"
    (repository / "link").symlink_to("/does-not-exist")
    assert "link" in graph.files()


@pytest.mark.asyncio
async def test_lifecycle_executes_reuses_and_blocks_premature_expansion(repository, tmp_path):
    config = Config(
        runtime_root=tmp_path / "runtime",
        repositories=[
            RepositoryConfig(name="org/repo", responsibility_paths=["area"]),
        ],
    )
    config_path = tmp_path / "config.toml"
    save_config(config, config_path)
    app = Application.build(config_path, agent_backend=AsyncMock())
    app.workflow.repository_indexer = None
    task = app.storage.create_task(
        Incident(
            external_id="1",
            source="test",
            repository="org/repo",
            environment="production",
            summary="bad value",
        )
    )
    app.storage.transition(task.task_id, TaskState.REPRODUCING)
    lifecycle = _TaskLifecycle(app.workflow, task.task_id, repository)
    await lifecycle.verification_plan(["area/seed.py"])
    with pytest.raises(RuntimeError, match="current ring"):
        await lifecycle.run_tests("true", ["area/outer.py"])
    (repository / "test_seed.py").write_text("from area.seed import value\nassert value == 1\n")
    refresh_graph(repository)
    command = "python test_seed.py"
    paths = ["area/seed.py", "test_seed.py"]
    first = await lifecycle.run_tests(command, paths)
    assert first["passed"] and not first["cached"]
    assert (await lifecycle.run_tests(command, paths))["cached"]
    assert not (await lifecycle.run_tests(command, paths, force=True))["cached"]
    with pytest.raises(RuntimeError, match="responsibility area"):
        await lifecycle.open_pr("premature")
    (repository / "area/seed.py").write_text("value = 100\n")
    refresh_graph(repository)
    with pytest.raises(RuntimeError, match="current passing"):
        await lifecycle.open_pr("stale")
    failed = await lifecycle.run_tests(command, paths)
    assert not failed["passed"] and not failed["cached"]
    app.workflow.repository_indexer = lambda _: SimpleNamespace(succeeded=False)
    with pytest.raises(RuntimeError, match="graph refresh"):
        await lifecycle.verification_plan([])


def test_upgrade_saved_configs_and_preserve_custom_models(tmp_path):
    path = tmp_path / "config.toml"
    save_config(Config(model=ModelConfig(name="gpt-5", reasoning="high")), path)
    migrated = load_config(path)
    assert (migrated.model.name, migrated.model.reasoning) == ("gpt-6-astra", "max")
    assert load_config(path) == migrated
    assert 'name = "gpt-6-astra"' in path.read_text()
    for options in (
        {"auto_upgrade": False},
        {"provider": "custom"},
        {"base_url": "http://localhost"},
        {"mode": "local", "base_url": "http://localhost"},
        {"name": "custom-model"},
    ):
        model = ModelConfig(**({"name": "gpt-5", "reasoning": "high"} | options))
        assert not upgrade_model(model)


@pytest.mark.asyncio
async def test_worker_reloads_policy_without_restart_and_retains_invalid_config(
    tmp_path, monkeypatch
):
    path = tmp_path / "config.toml"
    save_config(Config(runtime_root=tmp_path / "runtime", poll_interval_seconds=0.01), path)
    app = Application.build(path)
    worker = asyncio.create_task(app.workflow.run_worker())
    try:
        model = app.config.model
        policy_path = Path(__import__("src.config", fromlist=["__file__"]).__file__).with_name(
            "model-policy.json"
        )
        original = Path.read_text

        def read_text(file, *args, **kwargs):
            if file == policy_path:
                return json.dumps(
                    {"name": "test-next-release", "reasoning": "max", "predecessors": [model.name]}
                )
            return original(file, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_text)
        async with asyncio.timeout(2):
            while app.config.model.name != "test-next-release":
                await asyncio.sleep(0.01)
        assert app.agent.backend.config.model.name == "test-next-release"
        path.write_text("invalid TOML [")
        await asyncio.sleep(0.03)
        assert not worker.done()
        assert app.config.model.name == "test-next-release"
        path.write_text("")
        with pytest.raises(ValueError, match="missing or empty"):
            app.workflow.reload_model()
    finally:
        app.workflow.stop()
        await worker


def test_reload_switches_runtime_and_keeps_injected_backend(tmp_path):
    path = tmp_path / "config.toml"
    save_config(Config(runtime_root=tmp_path / "runtime"), path)
    app = Application.build(path)
    config = load_config(path)
    config.model.runtime = "subscription-cli"
    save_config(config, path)
    app.workflow.reload_model()
    assert type(app.agent.backend).__name__ == "SubscriptionCLIBackend"
    backend = AsyncMock()
    injected = Application.build(path, agent_backend=backend)
    config.model.max_tokens = 123
    save_config(config, path)
    injected.workflow.reload_model()
    assert injected.agent.backend is backend
    assert injected.config.model.max_tokens == 123
