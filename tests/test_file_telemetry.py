from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.telemetry import Telemetry


def test_file_telemetry_records_and_exports(tmp_path: Path) -> None:
    telemetry = Telemetry(tmp_path)
    telemetry.record("tool.shell", seconds=0.25, task_id="task_1")
    telemetry.record("tool.shell", success=False, seconds=0.5)
    assert telemetry.snapshot() == [
        {"name": "tool.shell", "calls": 2, "failures": 1, "seconds": 0.75}
    ]
    assert 'operation="tool.shell"' in telemetry.prometheus()
    assert not (tmp_path / "telemetry.sqlite3").exists()


def test_file_telemetry_imports_legacy_read_only(tmp_path: Path) -> None:
    database = tmp_path / "telemetry.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE metrics (name TEXT PRIMARY KEY, calls INTEGER, failures INTEGER, "
            "seconds REAL)"
        )
        connection.execute("INSERT INTO metrics VALUES ('agent.run', 2, 1, 3.5)")
    telemetry = Telemetry(tmp_path)
    assert telemetry.snapshot() == [
        {"name": "agent.run", "calls": 2, "failures": 1, "seconds": 3.5}
    ]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 1


def test_file_telemetry_validates_inputs(tmp_path: Path) -> None:
    telemetry = Telemetry(tmp_path)
    with pytest.raises(ValueError):
        telemetry.record("unknown")
    with pytest.raises(ValueError):
        telemetry.record("agent.run", seconds=-1)
    with pytest.raises(ValueError):
        telemetry.record("agent.run", task_id="bad id")


def test_file_telemetry_does_not_overwrite_existing_or_accept_corrupt_json(tmp_path: Path) -> None:
    database = tmp_path / "telemetry.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE metrics (name TEXT PRIMARY KEY, calls INTEGER, failures INTEGER, "
            "seconds REAL)"
        )
        connection.execute("INSERT INTO metrics VALUES ('agent.run', 1, 0, 1.0)")
    telemetry = Telemetry(tmp_path)
    telemetry.record("agent.run", seconds=2)
    restarted = Telemetry(tmp_path)
    assert restarted.snapshot()[0]["calls"] == 2
    restarted.database.write_text('{"schema_version": 1, "metrics": "bad"}')
    with pytest.raises(ValueError, match="invalid telemetry"):
        restarted.snapshot()


def test_file_telemetry_rejects_corrupt_legacy_and_missing_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corrupt = tmp_path / "telemetry.sqlite3"
    corrupt.write_text("not sqlite")
    with pytest.raises(ValueError, match="cannot read legacy telemetry"):
        Telemetry(tmp_path)

    empty = tmp_path / "empty"
    empty.mkdir()
    with sqlite3.connect(empty / "telemetry.sqlite3"):
        pass
    # An empty legacy database has no metrics table and is treated as an old empty install.
    assert Telemetry(empty).snapshot() == []

    legacy = empty / "telemetry.sqlite3"
    telemetry = Telemetry(empty)
    with sqlite3.connect(legacy) as connection:
        connection.execute(
            "CREATE TABLE metrics (name TEXT PRIMARY KEY, calls INTEGER, failures INTEGER, "
            "seconds REAL)"
        )
    telemetry.database.unlink(missing_ok=True)

    class AlreadyMigrated:
        class Transaction:
            def __enter__(self):
                return {"migrated": True}

            def __exit__(self, *args):
                return False

        def transaction(self):
            return self.Transaction()

    telemetry._metrics = AlreadyMigrated()
    telemetry._migrate_legacy(legacy)

    def fail_connect(*args, **kwargs):
        raise OSError("unreadable")

    monkeypatch.setattr("src.telemetry.sqlite3.connect", fail_connect)
    with pytest.raises(ValueError, match="cannot read legacy telemetry"):
        telemetry._migrate_legacy(legacy)
