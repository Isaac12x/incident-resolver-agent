from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.models import Incident, TaskEvent, TaskRecord, TaskState
from src.storage import Storage
from src.telemetry import Telemetry


def incident() -> Incident:
    return Incident(
        external_id="catalog-1",
        source="test",
        repository="example/service",
        environment="production",
        summary="failure",
    )


def test_catalog_recovers_without_folder_queue_or_snapshots(
    restore_task_state, tmp_path: Path
) -> None:
    storage = Storage(tmp_path / "custom-state")
    task = storage.create_task(incident())
    restore_task_state(storage, task.task_id, TaskState.INVESTIGATING)
    storage.append_event(task.task_id, TaskEvent(type="test.evidence"))
    shutil.rmtree(storage.tasks_root)
    restarted = Storage(storage.root)
    assert restarted.list_tasks("active")[0].state == TaskState.INVESTIGATING
    assert restarted.load_incident(task.task_id) == incident().model_copy(
        update={"received_at": restarted.load_incident(task.task_id).received_at}
    )
    assert restarted.events(task.task_id)[-1].type == "test.evidence"
    path = restarted.task_directory(task.task_id)
    assert path.parent.name == "active"
    assert json.loads((path / "state.json").read_text())["state"] == "investigating"
    (path / "state.json").write_text("corrupt snapshot")
    assert Storage(storage.root).load_task(task.task_id).state == TaskState.INVESTIGATING


def test_legacy_task_migration_preserves_state_events_and_sessions(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    task = TaskRecord(
        external_id="catalog-1",
        source="test",
        repository="example/service",
        environment="production",
        summary="failure",
        conversation_id="old-session",
        state=TaskState.WAITING_FOR_REVIEW,
    )
    folder = root / "tasks" / "waiting" / task.task_id
    folder.mkdir(parents=True)
    (folder / "state.json").write_text(task.model_dump_json())
    (folder / "input.json").write_text(incident().model_dump_json())
    (folder / "events.jsonl").write_text(TaskEvent(type="legacy.event").model_dump_json() + "\n")
    storage = Storage(root)
    assert storage.load_task(task.task_id).conversation_id == "old-session"
    assert storage.events(task.task_id)[0].type == "legacy.event"
    assert len(Storage(root).events(task.task_id)) == 1


def test_catalog_concurrent_submission_deduplicates_transactionally(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "runtime")
    payload = incident()
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: storage.create_task(payload).task_id, range(8)))
    assert len(set(ids)) == 1
    assert len(storage.list_tasks()) == 1
    with pytest.raises(FileNotFoundError):
        storage.catalog.save(storage.load_task(ids[0]).model_copy(update={"task_id": "missing"}))


def test_durable_workspace_identity_rejects_replacement_and_release(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "runtime")
    task = storage.create_task(incident())
    root = tmp_path / "worktree"
    root.mkdir()
    storage.catalog.verify_workspace(task.task_id, root)
    Storage(storage.root).catalog.verify_workspace(task.task_id, root)
    root.rename(tmp_path / "old")
    root.mkdir()
    with pytest.raises(ValueError, match="identity"):
        storage.catalog.verify_workspace(task.task_id, root)
    storage.catalog.register_workspace(task.task_id, root)
    storage.catalog.release_workspace(task.task_id)
    with pytest.raises(ValueError, match="identity"):
        storage.catalog.verify_workspace(task.task_id, root)


def test_telemetry_persists_failure_latency_and_rotates_without_payloads(tmp_path: Path) -> None:
    metrics = Telemetry(tmp_path)
    metrics.record("http.request", seconds=0.25)
    metrics.record("http.request", success=False, seconds=0.5)
    assert Telemetry(tmp_path).snapshot() == [
        {"name": "http.request", "calls": 2, "failures": 1, "seconds": 0.75}
    ]
    assert 'incident_harness_failures_total{operation="http.request"} 1' in metrics.prometheus()
    assert set(json.loads(metrics.log.read_text().splitlines()[0])) == {
        "time",
        "name",
        "success",
        "seconds",
        "task_id",
    }
    metrics.log.write_text("x" * 5_000_001)
    metrics.record("task.created", task_id="TASK_1")
    assert metrics.log.with_suffix(".jsonl.1").exists()
    for name, arguments in (
        ("user-secret", {}),
        ("http.request", {"seconds": -1}),
        ("http.request", {"task_id": "secret token"}),
    ):
        with pytest.raises(ValueError):
            metrics.record(name, **arguments)


def test_catalog_lease_survives_restart_and_expires(tmp_path, monkeypatch):
    storage = Storage(tmp_path)
    task = storage.create_task(incident())
    monkeypatch.setattr("src.task_catalog.time.time", lambda: 100)
    assert storage.catalog.acquire(task.task_id, "first", 10)
    other = Storage(tmp_path).catalog
    assert not other.acquire(task.task_id, "second", 10)
    other.release(task.task_id, "second")
    assert not other.acquire(task.task_id, "second", 10)
    assert other.acquire(task.task_id, "first", 10)
    monkeypatch.setattr("src.task_catalog.time.time", lambda: 111)
    assert other.acquire(task.task_id, "second", 10)
    storage.catalog.release(task.task_id, "first")
    assert not storage.catalog.acquire(task.task_id, "first", 10)
    other.release(task.task_id, "second")
    assert storage.catalog.acquire(task.task_id, "first", 10)


@pytest.mark.asyncio
async def test_workflow_lease_excludes_other_workers_and_releases_on_cancellation(tmp_path):
    import asyncio

    from src.config import Config
    from src.workflow import WorkflowEngine

    storage = Storage(tmp_path)
    task = storage.create_task(incident())
    first, second = WorkflowEngine.__new__(WorkflowEngine), WorkflowEngine.__new__(WorkflowEngine)
    entered = asyncio.Event()

    async def block(_):
        entered.set()
        await asyncio.Event().wait()

    for worker in (first, second):
        worker.storage = Storage(tmp_path)
        worker.config = Config(runtime_root=tmp_path)
        worker._process_unleased = block
    pending = asyncio.create_task(first.process(task.task_id))
    await entered.wait()
    assert (await second.process(task.task_id)).task_id == task.task_id
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert storage.catalog.acquire(task.task_id, "replacement", 10)


def test_metrics_require_auth_and_record_rejected_requests(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from src.config import Config
    from src.server import create_server

    monkeypatch.setenv("INCIDENT_AGENT_API_TOKEN", "test-token")
    config = Config(runtime_root=tmp_path)
    config.server.api_token_env = "INCIDENT_AGENT_API_TOKEN"
    config.server.require_api_auth = True
    storage = Storage(tmp_path)
    app = SimpleNamespace(config=config, storage=storage)
    client = TestClient(create_server(app, run_worker=False))
    assert client.get("/metrics").status_code == 401
    response = client.get("/metrics", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 200
    assert 'incident_harness_failures_total{operation="http.request"} 1' in response.text
    snapshot = client.get("/mcp/resources/metrics", headers={"Authorization": "Bearer test-token"})
    assert snapshot.json()[0]["calls"] == 2
