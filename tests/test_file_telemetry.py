from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.telemetry import Telemetry


def test_telemetry_records_in_shared_runtime_database(tmp_path: Path) -> None:
    telemetry = Telemetry(tmp_path)
    telemetry.record("tool.shell", seconds=0.25, task_id="task_1")
    telemetry.record("tool.shell", success=False, seconds=0.5)
    assert telemetry.snapshot() == [
        {"name": "tool.shell", "calls": 2, "failures": 1, "seconds": 0.75}
    ]
    assert (tmp_path / "runtime.sqlite3").exists()
    assert not (tmp_path / "telemetry.json").exists()


def test_telemetry_imports_json_before_legacy_and_ignores_late_sources(tmp_path: Path) -> None:
    (tmp_path / "telemetry.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "metrics": {"agent.run": {"calls": 2, "failures": 1, "seconds": 3.5}},
            }
        )
    )
    with sqlite3.connect(tmp_path / "telemetry.sqlite3") as db:
        db.execute(
            "CREATE TABLE metrics (name TEXT PRIMARY KEY, calls INTEGER, "
            "failures INTEGER, seconds REAL)"
        )
        db.execute("INSERT INTO metrics VALUES ('agent.run', 99, 0, 99.0)")
    telemetry = Telemetry(tmp_path)
    assert telemetry.snapshot()[0]["calls"] == 2
    (tmp_path / "telemetry.json").write_text("corrupt")
    assert Telemetry(tmp_path).snapshot()[0]["calls"] == 2


def test_telemetry_imports_legacy_read_only(tmp_path: Path) -> None:
    database = tmp_path / "telemetry.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE metrics (name TEXT PRIMARY KEY, calls INTEGER, "
            "failures INTEGER, seconds REAL)"
        )
        connection.execute("INSERT INTO metrics VALUES ('agent.run', 2, 1, 3.5)")
    assert Telemetry(tmp_path).snapshot()[0]["seconds"] == 3.5
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 1


def test_telemetry_rejects_corrupt_source_without_runtime_rows(tmp_path: Path) -> None:
    (tmp_path / "telemetry.json").write_text("invalid")
    with pytest.raises(ValueError, match="invalid telemetry"):
        Telemetry(tmp_path)
    if (tmp_path / "runtime.sqlite3").exists():
        with sqlite3.connect(tmp_path / "runtime.sqlite3") as db:
            assert db.execute("SELECT COUNT(*) FROM telemetry_metrics").fetchone()[0] == 0


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"schema_version": 2, "metrics": {}},
        {"schema_version": 1, "metrics": []},
        {"schema_version": 1, "metrics": {"agent.run": []}},
        {"schema_version": 1, "metrics": {"agent.run": {"calls": 1}}},
        {"schema_version": 1, "metrics": {"agent.run": {"calls": -1, "failures": 0, "seconds": 0}}},
        {"schema_version": 1, "metrics": {"agent.run": {"calls": 1, "failures": 2, "seconds": 0}}},
    ],
)
def test_telemetry_rejects_malformed_metric_documents(tmp_path: Path, document: object) -> None:
    (tmp_path / "telemetry.json").write_text(json.dumps(document))
    with pytest.raises(ValueError, match="invalid telemetry"):
        Telemetry(tmp_path)


def test_telemetry_legacy_missing_table_is_empty_and_corrupt_database_fails(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "telemetry.sqlite3"):
        pass
    assert Telemetry(tmp_path).snapshot() == []
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "telemetry.sqlite3").write_text("not sqlite")
    with pytest.raises(ValueError, match="cannot read legacy telemetry"):
        Telemetry(broken)


def test_telemetry_validates_inputs_and_concurrent_increments(tmp_path: Path) -> None:
    telemetry = Telemetry(tmp_path)
    with pytest.raises(ValueError):
        telemetry.record("unknown")
    with pytest.raises(ValueError):
        telemetry.record("agent.run", seconds=-1)
    with pytest.raises(ValueError):
        telemetry.record("agent.run", task_id="bad id")
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: telemetry.record("agent.run"), range(24)))
    assert Telemetry(tmp_path).snapshot()[0]["calls"] == 24
