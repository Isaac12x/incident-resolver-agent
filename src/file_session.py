"""SQLite-backed implementation of the OpenAI Agents SDK session protocol."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .sqlite_store import connect, transaction


class FileSession:
    session_settings: Any = None

    def __init__(self, session_id: str, db_path: str | Path = "sessions") -> None:
        self.session_id, self.session_settings = session_id, None
        supplied = Path(db_path)
        if supplied.suffix in {".sqlite3", ".db"}:
            sessions = supplied.parent / "sessions"
            if supplied.name == "runtime.sqlite3":
                self.database, self._legacy = supplied, supplied.parent / "sessions.sqlite3"
            else:
                self.database, self._legacy = supplied.with_name("runtime.sqlite3"), supplied
        else:
            self.database, sessions, self._legacy = (
                supplied.parent / "runtime.sqlite3",
                supplied,
                supplied.with_name("sessions.sqlite3"),
            )
        self.path = sessions / (hashlib.sha256(session_id.encode()).hexdigest() + ".json")
        self._ensure_schema()
        self._migrate_sources()

    def _ensure_schema(self) -> None:
        with transaction(self.database) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS sdk_items ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, "
                "item_json TEXT NOT NULL)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS sdk_items_session ON sdk_items(session_id, id)")

    def _migrate_sources(self) -> None:
        marker = "sdk-source-v1:" + hashlib.sha256(self.session_id.encode()).hexdigest()
        with transaction(self.database) as db:
            if db.execute("SELECT 1 FROM runtime_migrations WHERE name=?", (marker,)).fetchone():
                return
            if self.path.exists():
                try:
                    document = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as error:
                    raise ValueError(f"invalid session document: {self.path}") from error
                if (
                    not isinstance(document, dict)
                    or document.get("schema_version") != 1
                    or document.get("session_id") != self.session_id
                    or not isinstance(document.get("items"), list)
                ):
                    raise ValueError(f"invalid session document: {self.path}")
                for item in document["items"]:
                    db.execute(
                        "INSERT INTO sdk_items(session_id,item_json) VALUES (?,?)",
                        (
                            self.session_id,
                            json.dumps(item, ensure_ascii=False, separators=(",", ":")),
                        ),
                    )
            elif self._legacy.is_file():
                try:
                    with closing(
                        sqlite3.connect(self._legacy.resolve().as_uri() + "?mode=ro", uri=True)
                    ) as source:
                        rows = source.execute(
                            "SELECT message_data FROM agent_messages "
                            "WHERE session_id=? ORDER BY id ASC",
                            (self.session_id,),
                        ).fetchall()
                except sqlite3.OperationalError as error:
                    if "no such table" in str(error).lower():
                        rows = []
                    else:
                        raise ValueError(
                            f"cannot read legacy session database: {self._legacy}"
                        ) from error
                except (OSError, sqlite3.DatabaseError) as error:
                    raise ValueError(
                        f"cannot read legacy session database: {self._legacy}"
                    ) from error
                for (payload,) in rows:
                    try:
                        item = json.loads(payload)
                    except (TypeError, json.JSONDecodeError) as error:
                        raise ValueError(
                            f"invalid legacy session item for {self.session_id}"
                        ) from error
                    db.execute(
                        "INSERT INTO sdk_items(session_id,item_json) VALUES (?,?)",
                        (
                            self.session_id,
                            json.dumps(item, ensure_ascii=False, separators=(",", ":")),
                        ),
                    )
            db.execute("INSERT INTO runtime_migrations(name) VALUES (?)", (marker,))

    @staticmethod
    def _decode(rows: list[Any], session_id: str) -> list[Any]:
        try:
            return [json.loads(row[0]) for row in rows]
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid stored session item: {session_id}") from error

    def load_items(self, limit: int | None = None) -> list[Any]:
        """Read stored session items without requiring an event loop."""
        with connect(self.database) as db:
            if limit == 0:
                return []
            if limit is None or limit < 0:
                rows = db.execute(
                    "SELECT item_json FROM sdk_items WHERE session_id=? ORDER BY id",
                    (self.session_id,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT item_json FROM (SELECT item_json,id FROM sdk_items "
                    "WHERE session_id=? ORDER BY id DESC LIMIT ?) ORDER BY id",
                    (self.session_id, limit),
                ).fetchall()
        return self._decode(rows, self.session_id)

    async def get_items(self, limit: int | None = None) -> list[Any]:
        return await asyncio.to_thread(self.load_items, limit)

    async def add_items(self, items: list[Any]) -> None:
        if not items:
            return

        def append() -> None:
            with transaction(self.database) as db:
                for item in items:
                    db.execute(
                        "INSERT INTO sdk_items(session_id,item_json) VALUES (?,?)",
                        (
                            self.session_id,
                            json.dumps(item, ensure_ascii=False, separators=(",", ":")),
                        ),
                    )

        await asyncio.to_thread(append)

    async def pop_item(self) -> Any | None:
        def pop() -> Any | None:
            with transaction(self.database) as db:
                row = db.execute(
                    "SELECT id,item_json FROM sdk_items "
                    "WHERE session_id=? ORDER BY id DESC LIMIT 1",
                    (self.session_id,),
                ).fetchone()
                if row is None:
                    return None
                db.execute("DELETE FROM sdk_items WHERE id=?", (row["id"],))
                return self._decode([(row["item_json"],)], self.session_id)[0]

        return await asyncio.to_thread(pop)

    async def clear_session(self) -> None:
        def clear() -> None:
            with transaction(self.database) as db:
                db.execute("DELETE FROM sdk_items WHERE session_id=?", (self.session_id,))

        await asyncio.to_thread(clear)
