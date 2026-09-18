from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from src.config import ApplicationConfig, Config, RepositoryConfig
from src.models import Incident
from src.storage import Storage


def _checkout(path: Path, filename: str) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-qb", "main", str(path)], check=True)
    (path / filename).write_text("base\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
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
    return path


def test_application_task_deduplicates_across_member_repositories_and_recovers(
    tmp_path: Path,
) -> None:
    api = _checkout(tmp_path / "api", "api.py")
    web = _checkout(tmp_path / "web", "web.js")
    config = Config(
        runtime_root=tmp_path / "runtime",
        repositories=[
            RepositoryConfig(name="org/api", local_path=api, publish_mode="local"),
            RepositoryConfig(name="org/web", local_path=web, publish_mode="local"),
        ],
        applications=[
            ApplicationConfig(
                name="storefront", services=["api", "web"], repositories=["org/api", "org/web"]
            )
        ],
    )
    storage = Storage(config.runtime_root)
    incident = Incident(
        external_id="incident-1",
        source="alerts",
        service="api",
        application="storefront",
        environment="production",
        summary="broken checkout",
    )
    task = storage.create_task(incident, config)
    duplicate = storage.create_task(incident.model_copy(update={"service": "web"}), config)
    assert duplicate.task_id == task.task_id
    assert task.repository_names() == ["org/api", "org/web"]

    paths = storage.create_application_worktrees(task, config)
    assert set(paths) == set(task.repositories)
    assert paths["org/api"] != paths["org/web"]
    for repository, path in paths.items():
        (path / "change.txt").write_text(repository)
        sha = storage.commit_worktree(task, f"fix {repository}", repository)
        assert len(sha) == 40
        assert repository in storage.worktree_diff(task, repository)

    restarted = Storage(config.runtime_root)
    recovered = restarted.load_task(task.task_id)
    for repository in recovered.repositories:
        path = restarted.repository_worktree(recovered, repository)
        restarted.catalog.verify_workspace(recovered.task_id, path, repository)
    restarted.cleanup_application_worktrees(recovered)
    assert not restarted.repository_worktree(recovered, "org/api").exists()


def test_config_rejects_repository_slug_collisions() -> None:
    with pytest.raises(ValueError, match="collide"):
        Config(
            repositories=[RepositoryConfig(name="org/a--b"), RepositoryConfig(name="org--a/b")]
        )


def test_catalog_upgrades_legacy_workspace_identity_schema(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE catalog_workspaces (task_id TEXT PRIMARY KEY, root TEXT NOT NULL, "
            "device INTEGER NOT NULL, inode INTEGER NOT NULL, active INTEGER NOT NULL)"
        )
    from src.task_catalog import TaskCatalog

    TaskCatalog(path)
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(catalog_workspaces)")}
    assert "repository" in columns


def test_catalog_rejects_replaced_member_workspace(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "runtime")
    task = storage.create_task(
        Incident(
            external_id="identity-1",
            source="alerts",
            repository="org/api",
            environment="production",
            summary="failure",
        )
    )
    original = tmp_path / "original"
    replacement = tmp_path / "replacement"
    original.mkdir()
    replacement.mkdir()
    storage.catalog.register_workspace(task.task_id, original)
    with pytest.raises(ValueError, match="identity"):
        storage.catalog.verify_workspace(task.task_id, replacement)


def test_repository_only_task_keeps_legacy_layout(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "runtime")
    task = storage.create_task(
        Incident(
            external_id="legacy-1",
            source="alerts",
            repository="org/api",
            environment="production",
            summary="failure",
        )
    )
    assert task.repositories == {}
    assert storage.repository_worktree(task) == storage.root / "worktrees" / task.task_id
