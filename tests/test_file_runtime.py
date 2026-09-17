"""Runtime persistence contracts independent of the underlying file layout."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from src.models import Incident, TaskState
from src.storage import Storage


def test_fresh_runtime_uses_files_and_can_resume_in_another_process(tmp_path):
    storage = Storage(tmp_path)
    task = storage.create_task(
        Incident(
            external_id="files-only",
            source="test",
            repository="example/service",
            environment="production",
            summary="Persist the incident without a database",
        )
    )
    storage.transition(task.task_id, TaskState.COLLECTING_CONTEXT)
    storage.add_message(task.conversation_id, "user", "preserve this evidence")
    storage.record_incident_history(task.task_id, storage.load_incident(task.task_id))
    code = """
import json, sys
from src.storage import Storage
storage = Storage(sys.argv[1])
task = storage.load_task(sys.argv[2])
print(json.dumps({"state": task.state.value,
                  "messages": storage.messages(task.conversation_id),
                  "events": [event.type for event in storage.events(task.task_id)]}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), task.task_id],
        capture_output=True,
        text=True,
        check=True,
    )
    recovered = json.loads(result.stdout)
    assert recovered["state"] == "collecting_context"
    assert recovered["messages"] == [["user", "preserve this evidence"]]
    assert recovered["events"] == ["task.received", "task.collecting_context"]
    assert not list(tmp_path.rglob("*.sqlite*"))
    assert not list(tmp_path.rglob("*.db"))


def test_runtime_scaffolds_do_not_create_sqlite_files():
    from pathlib import Path

    for name in ("src/runtime.tree", ".seed/specs/runtime.tree"):
        assert ".sqlite" not in Path(name).read_text()


def test_illegal_transition_preserves_state_and_event_journal(tmp_path):
    storage = Storage(tmp_path)
    task = storage.create_task(
        Incident(
            external_id="illegal-edge",
            source="test",
            repository="example/service",
            environment="production",
            summary="Do not bypass verification",
        )
    )
    before = storage.events(task.task_id)
    with pytest.raises(ValueError, match="illegal lifecycle transition"):
        storage.transition(task.task_id, TaskState.PUBLISHING_PR)
    restarted = Storage(tmp_path)
    assert restarted.load_task(task.task_id).state == TaskState.RECEIVED
    assert restarted.events(task.task_id) == before


def test_changed_alert_payload_keeps_group_duplicate_reference_across_restart(tmp_path):
    storage = Storage(tmp_path)
    first = storage.record_observability_event(
        "grafana", {"groupKey": "service-down", "external_id": "alert-1", "message": "first"}
    )
    restarted = Storage(tmp_path)
    repeated = restarted.record_observability_event(
        "grafana", {"groupKey": "service-down", "external_id": "alert-1", "message": "again"}
    )
    assert repeated["duplicate"] is True
    assert repeated["duplicate_of"] == first["event_id"]
    assert repeated["event_id"] != first["event_id"]
    assert restarted.list_observability_events()[0]["duplicate_of"] == first["event_id"]
