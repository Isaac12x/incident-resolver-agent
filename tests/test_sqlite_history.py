from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.models import Incident
from src.storage import Storage


def incident() -> Incident:
    return Incident(
        external_id="history-1",
        source="pagerduty",
        repository="org/service",
        environment="production",
        summary="Database timeout",
    )


def write_legacy_history(path: Path, row: tuple[object, ...]) -> bytes:
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE incident_history (task_id, external_id, source, repository, "
            "environment, summary, description, root_cause, outcome, created_at)"
        )
        db.execute("INSERT INTO incident_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
    return path.read_bytes()


def test_history_is_row_based_and_survives_restart(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    storage.record_incident_history("task-1", incident(), root_cause="connection pool")
    storage.record_incident_history("task-1", incident(), outcome="fixed")
    assert storage.incident_history(labeled_only=True)[0]["outcome"] == "fixed"
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM incident_history").fetchone()[0] == 1
    assert Storage(tmp_path).incident_history()[0]["root_cause"] == "connection pool"


def test_current_history_json_precedes_legacy_and_source_stays_unchanged(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    row = ("legacy", "old", "legacy", "org/old", "prod", "old", "", None, None, "2025-01-01")
    legacy = tmp_path / "sessions.sqlite3"
    before = write_legacy_history(legacy, row)
    current = {
        "version": 1,
        "history": [
            {
                "task_id": "current",
                "external_id": "new",
                "source": "sentry",
                "repository": "org/new",
                "environment": "prod",
                "summary": "new",
                "description": "",
                "root_cause": "known",
                "outcome": None,
                "created_at": "2025-01-02",
            }
        ],
    }
    (sessions / "history.json").write_text(json.dumps(current))
    assert Storage(tmp_path).incident_history()[0]["task_id"] == "current"
    assert legacy.read_bytes() == before


def test_history_source_marker_prevents_resurrection_after_source_removal(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "history.json").write_text(json.dumps({"version": 1, "history": []}))
    Storage(tmp_path)
    (sessions / "history.json").unlink()
    write_legacy_history(
        tmp_path / "sessions.sqlite3",
        ("legacy", "old", "legacy", "org/old", "prod", "old", "", None, None, "2025-01-01"),
    )
    assert Storage(tmp_path).incident_history() == []


def test_corrupt_history_json_fails_closed(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "history.json").write_text("{")
    with pytest.raises(ValueError, match="cannot read session snapshot"):
        Storage(tmp_path)


def test_unknown_history_schema_fails_closed(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "history.json").write_text(json.dumps({"version": 2, "history": []}))
    with pytest.raises(ValueError, match="invalid session snapshot"):
        Storage(tmp_path)


def test_non_object_observability_payload_fails_closed(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "observability.json").write_text(
        json.dumps(
            {
                "version": 1,
                "events": [
                    {
                        "event_id": "e",
                        "source": "test",
                        "group_key": "g",
                        "fingerprint": "f",
                        "payload": ["invalid"],
                        "received_at": "2025-01-01",
                        "duplicate_of": None,
                        "task_id": None,
                    }
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="invalid session snapshot"):
        Storage(tmp_path)
