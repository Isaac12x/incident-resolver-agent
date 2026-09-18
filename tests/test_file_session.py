from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.file_session import FileSession


def snapshot_path(root: Path, session_id: str) -> Path:
    return root / "sessions" / (hashlib.sha256(session_id.encode()).hexdigest() + ".json")


def legacy_database(path: Path, session_id: str, items: list[object]) -> bytes:
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS agent_messages ("
            "id INTEGER PRIMARY KEY, session_id TEXT, message_data TEXT)"
        )
        offset = db.execute("SELECT COALESCE(MAX(id), 0) FROM agent_messages").fetchone()[0]
        for index, item in enumerate(items, 1):
            db.execute(
                "INSERT INTO agent_messages VALUES (?, ?, ?)",
                (offset + index, session_id, json.dumps(item)),
            )
    return path.read_bytes()


@pytest.mark.asyncio
async def test_sqlite_round_trip_limits_and_index_order(tmp_path: Path) -> None:
    session = FileSession("task/one", tmp_path / "sessions")
    await session.add_items([{"index": i} for i in range(4)])
    assert await session.get_items() == [{"index": i} for i in range(4)]
    assert await session.get_items(2) == [{"index": 2}, {"index": 3}]
    assert await session.pop_item() == {"index": 3}
    await session.clear_session()
    assert await session.get_items() == []
    assert (tmp_path / "runtime.sqlite3").is_file()
    assert not list((tmp_path / "sessions").glob("*.json"))
    await session.add_items([])
    assert await session.get_items(0) == []
    assert await session.get_items(-1) == []
    assert await session.pop_item() is None


@pytest.mark.asyncio
async def test_corrupt_stored_item_fails_closed_and_remains_transactional(tmp_path: Path) -> None:
    session = FileSession("corrupt", tmp_path / "sessions")
    await session.add_items([{"valid": True}])
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as db:
        db.execute("UPDATE sdk_items SET item_json='not-json' WHERE session_id='corrupt'")
    with pytest.raises(ValueError, match="invalid stored session item"):
        await session.get_items()
    with pytest.raises(ValueError, match="invalid stored session item"):
        await session.pop_item()
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as db:
        assert (
            db.execute("SELECT COUNT(*) FROM sdk_items WHERE session_id='corrupt'").fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
async def test_current_json_precedes_legacy_and_sources_are_unchanged(tmp_path: Path) -> None:
    database = tmp_path / "sessions.sqlite3"
    before = legacy_database(database, "same", [{"source": "legacy"}])
    path = snapshot_path(tmp_path, "same")
    path.parent.mkdir()
    path.write_text(
        json.dumps({"schema_version": 1, "session_id": "same", "items": [{"source": "json"}]})
    )
    assert await FileSession("same", tmp_path / "sessions").get_items() == [{"source": "json"}]
    assert database.read_bytes() == before


@pytest.mark.asyncio
async def test_removed_source_cannot_resurrect_and_empty_session_is_authoritative(
    tmp_path: Path,
) -> None:
    path = snapshot_path(tmp_path, "same")
    path.parent.mkdir()
    path.write_text(
        json.dumps({"schema_version": 1, "session_id": "same", "items": [{"source": "json"}]})
    )
    FileSession("same", tmp_path / "sessions")
    path.unlink()
    legacy_database(tmp_path / "sessions.sqlite3", "same", [{"source": "legacy"}])
    assert await FileSession("same", tmp_path / "sessions").get_items() == [{"source": "json"}]

    empty = FileSession("empty", tmp_path / "sessions")
    legacy_database(tmp_path / "sessions.sqlite3", "empty", [{"source": "legacy"}])
    assert await empty.get_items() == []
    assert await FileSession("empty", tmp_path / "sessions").get_items() == []


@pytest.mark.asyncio
async def test_corrupt_json_rolls_back_import(tmp_path: Path) -> None:
    path = snapshot_path(tmp_path, "broken")
    path.parent.mkdir()
    path.write_text("{")
    with pytest.raises(ValueError, match="invalid session document"):
        FileSession("broken", tmp_path / "sessions")
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM sdk_items").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT COUNT(*) FROM runtime_migrations WHERE name LIKE 'sdk-source-%'"
            ).fetchone()[0]
            == 0
        )


def test_relocation_and_constructor_aliases_reuse_same_runtime(tmp_path: Path) -> None:
    session = FileSession("moved", tmp_path / "sessions")
    asyncio.run(session.add_items([{"value": 1}]))
    assert asyncio.run(FileSession("moved", tmp_path / "runtime.sqlite3").get_items()) == [
        {"value": 1}
    ]
    assert asyncio.run(FileSession("moved", tmp_path / "sessions").get_items()) == [{"value": 1}]


def test_runtime_database_can_be_relocated_without_resurrection(tmp_path: Path) -> None:
    session = FileSession("relocated", tmp_path / "sessions")
    asyncio.run(session.add_items([{"value": 1}]))
    moved = tmp_path / "moved"
    moved.mkdir()
    shutil.move(tmp_path / "runtime.sqlite3", moved / "runtime.sqlite3")
    assert asyncio.run(FileSession("relocated", moved / "runtime.sqlite3").get_items()) == [
        {"value": 1}
    ]


def test_concurrent_session_appends_are_retained(tmp_path: Path) -> None:
    def append(index: int) -> None:
        asyncio.run(FileSession("shared", tmp_path / "sessions").add_items([{"index": index}]))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(24)))
    items = asyncio.run(FileSession("shared", tmp_path / "sessions").get_items())
    assert sorted(item["index"] for item in items) == list(range(24))


@pytest.mark.asyncio
async def test_malformed_legacy_is_rejected_without_mutation(tmp_path: Path) -> None:
    database = tmp_path / "sessions.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE agent_messages (id INTEGER, session_id TEXT, message_data TEXT)")
        db.execute("INSERT INTO agent_messages VALUES (1, 'bad', 'not json')")
    before = database.read_bytes()
    with pytest.raises(ValueError, match="invalid legacy session item"):
        FileSession("bad", database)
    assert database.read_bytes() == before


@pytest.mark.asyncio
async def test_missing_or_invalid_legacy_sources_fail_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite3"
    assert await FileSession("empty", missing).get_items(-1) == []
    no_table = tmp_path / "no-table.sqlite3"
    with sqlite3.connect(no_table):
        pass
    assert await FileSession("no-table", no_table).get_items() == []
    invalid = tmp_path / "invalid.sqlite3"
    invalid.write_bytes(b"not sqlite")
    with pytest.raises(ValueError, match="cannot read legacy session database"):
        FileSession("bad", invalid)


def test_session_snapshot_schema_and_identity_are_validated(tmp_path: Path) -> None:
    path = snapshot_path(tmp_path, "schema")
    path.parent.mkdir()
    path.write_text(json.dumps({"schema_version": 2, "session_id": "schema", "items": []}))
    with pytest.raises(ValueError, match="invalid session document"):
        FileSession("schema", tmp_path / "sessions")
    path.write_text(json.dumps({"schema_version": 1, "session_id": "other", "items": []}))
    with pytest.raises(ValueError, match="invalid session document"):
        FileSession("schema", tmp_path / "sessions")
