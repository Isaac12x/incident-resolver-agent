"""Recovery contracts exercised across real subprocess and storage boundaries."""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from src.agent import AgentRunContext, SubscriptionCLIBackend
from src.config import Config, ModelConfig
from src.models import Incident, ReviewComment, TaskState
from src.storage import Storage
from src.tools import WorkspaceTools
from src.workflow import WorkflowEngine


def create_task(storage):
    return storage.create_task(
        Incident(
            external_id="crash",
            source="test",
            repository="example/service",
            environment="production",
            summary="Resume interrupted incident",
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exit", "cancel", "timeout"])
async def test_cli_identity_persisted_before_interruption_and_reused(
    tmp_path, monkeypatch, failure
):
    storage = Storage(tmp_path / "state")
    task = create_task(storage)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = Config(model=ModelConfig(runtime="subscription-cli", show_execution_details=False))
    if failure == "timeout":
        config.model.tool_timeout_seconds = 0.2
    backend = SubscriptionCLIBackend(config)
    saved = asyncio.Event()
    children = []
    commands = []
    real_spawn = asyncio.create_subprocess_exec

    async def spawn(*command, **kwargs):
        commands.append(command)
        code = (
            "import sys, json, time; sys.stdin.read(); "
            "print(json.dumps({'type':'thread.started',"
            "'thread_id':'durable-thread'}), flush=True); "
        )
        if len(commands) == 1:
            code += "time.sleep(0.05); sys.exit(7)" if failure == "exit" else "time.sleep(60)"
        else:
            result = json.dumps({"summary": "resumed", "waiting_for_external_event": True})
            event = {"type": "item.completed", "item": {"type": "agent_message", "text": result}}
            code += f"print({json.dumps(event)!r})"
        child = await real_spawn(sys.executable, "-c", code, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    def save(identity):
        latest = storage.load_task(task.task_id)
        latest.backend_session_id = identity
        storage.save_task(latest)
        saved.set()

    def context(record):
        return AgentRunContext(
            task=record,
            session_id=record.agent_session_id,
            session_db=storage.root / "sessions.sqlite3",
            lifecycle=SimpleNamespace(),
            save_backend_session=save,
            memory_writer=lambda _: None,
        )

    pending = asyncio.create_task(
        backend(
            "instructions",
            "prompt",
            WorkspaceTools(workspace),
            [],
            run_context=context(task),
        )
    )
    await asyncio.wait_for(saved.wait(), timeout=5)
    # The session is visible to a fresh process before a final model result exists.
    restarted = Storage(storage.root)
    resumed = restarted.load_task(task.task_id)
    assert resumed.backend_session_id == "durable-thread"
    if failure == "cancel":
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        with pytest.raises(RuntimeError, match="exited 7" if failure == "exit" else "timed out"):
            await pending
    assert children[0].returncode is not None
    config.model.tool_timeout_seconds = 10
    result = await backend(
        "instructions",
        "resume",
        WorkspaceTools(workspace),
        [],
        run_context=context(resumed),
    )
    assert result["summary"] == "resumed"
    assert commands[1][3:5] == ("resume", "durable-thread")


@pytest.mark.asyncio
async def test_worker_rediscovers_review_after_dead_owner_lease_expires(
    restore_task_state, tmp_path
):
    config = Config(runtime_root=tmp_path, poll_interval_seconds=0.01)
    storage = Storage(tmp_path)
    task = create_task(storage)
    restore_task_state(
        storage,
        task.task_id,
        TaskState.WAITING_FOR_REVIEW,
        pending_review_comments=[ReviewComment(id=1, author="owner", body="fix")],
    )
    assert storage.catalog.acquire(task.task_id, "dead-worker", 0.08)
    restarted = Storage(tmp_path)
    worker = WorkflowEngine(config, restarted, None, None, None)
    processed = []

    async def resume(task_id):
        record = restarted.load_task(task_id)
        assert record.pending_review_comments[0].body == "fix"
        processed.append(task_id)
        worker.stop()
        return record

    worker._process_unleased = resume
    await asyncio.wait_for(worker.run_worker(), timeout=2)
    assert processed == [task.task_id]
    assert restarted.load_task(task.task_id).attempts == 0


@pytest.mark.asyncio
async def test_worker_polls_durable_intake_without_in_memory_wakeup(tmp_path):
    config = Config(runtime_root=tmp_path, poll_interval_seconds=0.01)
    storage = Storage(tmp_path)
    worker = WorkflowEngine(config, storage, None, None, None)
    processed = []

    async def resume(task_id):
        processed.append(task_id)
        worker.stop()
        return storage.load_task(task_id)

    worker._process_unleased = resume
    running = asyncio.create_task(worker.run_worker())
    await asyncio.sleep(0.03)
    # Simulates a submitter crashing between committing intake and enqueueing its wakeup.
    task = create_task(Storage(tmp_path))
    await asyncio.wait_for(running, timeout=2)
    assert processed == [task.task_id]


@pytest.mark.asyncio
async def test_worker_cancellation_stops_owned_job_and_releases_lease(tmp_path):
    config = Config(runtime_root=tmp_path, poll_interval_seconds=0.01)
    storage = Storage(tmp_path)
    task = create_task(storage)
    worker = WorkflowEngine(config, storage, None, None, None)
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def interrupted(task_id):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    worker._process_unleased = interrupted
    running = asyncio.create_task(worker.run_worker())
    await asyncio.wait_for(entered.wait(), timeout=2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert stopped.is_set()
    assert storage.catalog.acquire(task.task_id, "replacement", 10)
    assert storage.load_task(task.task_id).state == TaskState.RECEIVED


@pytest.mark.asyncio
async def test_abrupt_process_death_resumes_last_checkpoint(tmp_path):
    storage = Storage(tmp_path)
    task = create_task(storage)
    code = """
import os, sys
from src.storage import Storage
from src.models import TaskState
storage = Storage(sys.argv[1])
task_id = sys.argv[2]
worktree = storage.root / 'worktrees' / task_id
worktree.mkdir()
(worktree / 'fix.py').write_text('preserved edit')
storage.catalog.register_workspace(task_id, worktree)
storage.append_task_memory(task_id, 'Investigation complete; continue implementation.')
for state in (TaskState.COLLECTING_CONTEXT, TaskState.INVESTIGATING, TaskState.REPRODUCING):
    storage.transition(task_id, state)
storage.transition(task_id, TaskState.IMPLEMENTING, backend_session_id='saved-thread')
storage.catalog.acquire(task_id, 'crashed-process', 0.1)
os._exit(9)
"""
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, str(tmp_path), task.task_id
    )
    assert await child.wait() == 9
    restarted = Storage(tmp_path)
    worker = WorkflowEngine(
        Config(runtime_root=tmp_path, poll_interval_seconds=0.01), restarted, None, None, None
    )
    resumed = []

    async def resume(task_id):
        record = restarted.load_task(task_id)
        assert record.state == TaskState.IMPLEMENTING
        assert record.backend_session_id == "saved-thread"
        assert "Investigation complete" in restarted.read_task_memory(task_id)
        worktree = restarted.root / "worktrees" / task_id
        restarted.catalog.verify_workspace(task_id, worktree)
        assert (worktree / "fix.py").read_text() == "preserved edit"
        resumed.append(task_id)
        worker.stop()
        return record

    worker._process_unleased = resume
    await asyncio.wait_for(worker.run_worker(), timeout=2)
    assert resumed == [task.task_id]
