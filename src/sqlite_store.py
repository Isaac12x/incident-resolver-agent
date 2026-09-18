"""Small, shared SQLite primitives for durable runtime state."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def _prepare(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30.0)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 30000")
        db.execute("PRAGMA foreign_keys = ON")
        deadline = time.monotonic() + 30.0
        while True:
            try:
                db.execute("PRAGMA journal_mode = WAL")
                break
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        db.execute("PRAGMA synchronous = FULL")
        db.execute("CREATE TABLE IF NOT EXISTS runtime_migrations (name TEXT PRIMARY KEY)")
        return db
    except BaseException:
        db.close()
        raise


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a configured runtime database and commit or roll back its work."""
    db = _prepare(Path(path))
    try:
        yield db
    except BaseException:
        db.rollback()
        raise
    else:
        db.commit()
    finally:
        db.close()


@contextmanager
def transaction(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a configured database and acquire its write transaction."""
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        yield db
