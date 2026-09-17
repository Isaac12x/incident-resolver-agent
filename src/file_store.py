"""Small, durable JSON documents with inter-process transactions."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path


class JsonFile:
    def __init__(self, path: Path, default_factory: Callable[[], dict]) -> None:
        self.path = Path(path)
        self.default_factory = default_factory
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read_unlocked(self) -> dict:
        if not self.path.exists():
            return self.default_factory()
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON store root must be an object")
        return value

    def read(self) -> dict:
        with self._lock(shared=True):
            return self._read_unlocked()

    @contextmanager
    def _lock(self, *, shared: bool = False) -> Iterator[None]:
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @contextmanager
    def transaction(self) -> Iterator[dict]:
        with self._lock():
            value = self._read_unlocked()
            original = json.dumps(value, sort_keys=True, default=str)
            yield value
            if self.path.exists() and json.dumps(value, sort_keys=True, default=str) == original:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.path.parent,
                    prefix="." + self.path.name + ".",
                    delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    json.dump(value, handle, indent=2, ensure_ascii=False, default=str)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(self.path)
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
