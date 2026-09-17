from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.file_session import FileSession


@pytest.mark.asyncio
async def test_file_session_round_trip_and_limits(tmp_path: Path) -> None:
    session = FileSession("task/one", tmp_path / "sessions.sqlite3")
    await session.add_items([{"role": "user", "content": str(i)} for i in range(3)])
    assert await session.get_items() == [{"role": "user", "content": str(i)} for i in range(3)]
    assert await session.get_items(2) == [
        {"role": "user", "content": "1"},
        {"role": "user", "content": "2"},
    ]
    assert await session.pop_item() == {"role": "user", "content": "2"}
    await session.clear_session()
    assert await session.get_items() == []
    assert list((tmp_path / "sessions").glob("*.json"))
    await session.add_items([])
    assert await session.get_items(0) == []


@pytest.mark.asyncio
async def test_file_session_imports_legacy_without_mutating_database(tmp_path: Path) -> None:
    database = tmp_path / "sessions.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE agent_messages ("
            "id INTEGER PRIMARY KEY, session_id TEXT, message_data TEXT);"
        )
        connection.execute(
            "INSERT INTO agent_messages VALUES (1, 'legacy', '{\"role\": \"user\"}')"
        )
    session = FileSession("legacy", database)
    assert await session.get_items() == [{"role": "user"}]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_messages").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_file_session_rejects_malformed_legacy_item(tmp_path: Path) -> None:
    database = tmp_path / "sessions.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE agent_messages (id INTEGER, session_id TEXT, message_data TEXT)"
        )
        connection.execute("INSERT INTO agent_messages VALUES (1, 'bad', 'not json')")
    with pytest.raises(ValueError, match="invalid legacy session item"):
        FileSession("bad", database)


@pytest.mark.asyncio
async def test_file_session_mutators_reject_corrupt_schema(tmp_path: Path) -> None:
    session = FileSession("bad", tmp_path / "sessions")
    session.path.write_text('{"schema_version": 1, "items": "lost"}')
    with pytest.raises(ValueError, match="invalid session document"):
        await session.add_items([{"role": "user"}])
    with pytest.raises(ValueError, match="invalid session document"):
        await session.pop_item()
    with pytest.raises(ValueError, match="invalid session document"):
        await session.clear_session()


@pytest.mark.asyncio
async def test_clear_empty_session_does_not_resurrect_legacy(tmp_path: Path) -> None:
    database = tmp_path / "sessions.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE agent_messages (id INTEGER, session_id TEXT, message_data TEXT)"
        )
        connection.execute(
            "INSERT INTO agent_messages VALUES (1, 'legacy', '{\"role\": \"user\"}')"
        )
    session = FileSession("legacy", database)
    await session.clear_session()
    assert await FileSession("legacy", database).get_items() == []


@pytest.mark.asyncio
async def test_invalid_session_document_fails_visibly(tmp_path: Path) -> None:
    session = FileSession("bad", tmp_path / "sessions")
    session.path.write_text('{"schema_version": 1, "items": "lost"}')
    with pytest.raises(ValueError, match="invalid session document"):
        await session.get_items()


def test_separate_sessions_concurrent_appends_are_retained(tmp_path: Path) -> None:
    def append(index: int) -> None:
        async def operation() -> None:
            await FileSession("shared", tmp_path / "sessions").add_items([{"index": index}])

        import asyncio

        asyncio.run(operation())

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(24)))
    import asyncio

    items = asyncio.run(FileSession("shared", tmp_path / "sessions").get_items())
    assert sorted(item["index"] for item in items) == list(range(24))


@pytest.mark.asyncio
async def test_file_session_handles_missing_or_invalid_legacy(tmp_path: Path) -> None:
    session = FileSession("empty", tmp_path / "missing.sqlite3")
    assert await session.get_items(-1) == []
    database = tmp_path / "invalid.sqlite3"
    database.write_text("not sqlite")
    with pytest.raises(ValueError, match="cannot read legacy"):
        FileSession("bad", database)
