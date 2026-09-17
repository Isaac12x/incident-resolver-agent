"""File-backed implementation of the OpenAI Agents SDK session protocol."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .file_store import JsonFile


class FileSession:
    """Durable session history stored as one JSON document per session.

    ``db_path`` is retained as a compatibility argument for callers that used
    ``SQLiteSession``. Existing SDK rows are imported read-only when present.
    """

    session_settings: Any = None

    def __init__(self, session_id: str, db_path: str | Path = "sessions") -> None:
        self.session_id = session_id
        self.session_settings = None
        legacy = Path(db_path)
        if legacy.suffix in {".sqlite3", ".db"}:
            root = legacy.parent / "sessions"
            legacy_database = legacy
        else:
            root = legacy
            legacy_database = legacy.with_name("sessions.sqlite3")
        self.path = root / (hashlib.sha256(session_id.encode()).hexdigest() + ".json")
        self._store = JsonFile(
            self.path, lambda: {"schema_version": 1, "session_id": session_id, "items": []}
        )
        self._migrate_legacy(legacy_database)

    def _migrate_legacy(self, database: Path | None) -> None:
        if database is None or not database.is_file() or self.path.exists():
            return
        try:
            connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                rows = connection.execute(
                    "SELECT message_data FROM agent_messages WHERE session_id = ? ORDER BY id ASC",
                    (self.session_id,),
                ).fetchall()
            finally:
                connection.close()
            items = []
            for (payload,) in rows:
                try:
                    items.append(json.loads(payload))
                except (TypeError, json.JSONDecodeError) as error:
                    raise ValueError(
                        f"invalid legacy session item for {self.session_id}"
                    ) from error
            with self._store.transaction() as document:
                if document.get("migrated"):
                    return
                if items:
                    document["items"] = items
                document["migrated"] = True
        except sqlite3.OperationalError as error:
            if "no such table" not in str(error).lower():
                raise ValueError(f"cannot read legacy session database: {database}") from error
        except sqlite3.DatabaseError as error:
            raise ValueError(f"cannot read legacy session database: {database}") from error
        except OSError as error:
            raise ValueError(f"cannot read legacy session database: {database}") from error

    def _items(self) -> list[Any]:
        document = self._store.read()
        self._validate(document)
        return document["items"]

    def _validate(self, document: dict[str, Any]) -> None:
        if document.get("schema_version") != 1 or not isinstance(document.get("items"), list):
            raise ValueError(f"invalid session document: {self.path}")

    async def get_items(self, limit: int | None = None) -> list[Any]:
        def read() -> list[Any]:
            items = self._items()
            if limit is None:
                return items
            if limit > 0:
                return items[-limit:]
            if limit == 0:
                return []
            return items

        return await asyncio.to_thread(read)

    async def add_items(self, items: list[Any]) -> None:
        if not items:
            return

        def append() -> None:
            with self._store.transaction() as document:
                self._validate(document)
                document["migrated"] = True
                document.setdefault("items", []).extend(items)

        await asyncio.to_thread(append)

    async def pop_item(self) -> Any | None:
        def pop() -> Any | None:
            with self._store.transaction() as document:
                self._validate(document)
                document["migrated"] = True
                items = document.setdefault("items", [])
                return items.pop() if items else None

        return await asyncio.to_thread(pop)

    async def clear_session(self) -> None:
        def clear() -> None:
            with self._store.transaction() as document:
                self._validate(document)
                document["migrated"] = True
                document["items"] = []

        await asyncio.to_thread(clear)
