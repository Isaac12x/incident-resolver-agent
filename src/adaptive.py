"""Persistent, bounded tool selection for incident runs.

The policy is deliberately small: a contextual UCB bandit is sufficient to learn
which already trusted tool works best for a recurring incident shape.  It never
downloads code or invents tool names; callers provide the catalog and executor.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import math
import os
import random
import threading
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FILE_LOCKS: dict[str, threading.RLock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class ToolOutcome:
    tool: str
    success: bool
    reward: float
    attempts: int
    value: Any = None
    error: str | None = None


class ToolPolicy:
    """A durable contextual UCB1 policy with atomic writes and bounded values."""

    def __init__(
        self, path: Path | str, *, exploration: float = 1.2, seed: int | None = None
    ) -> None:
        self.path = Path(path)
        self.exploration = max(0.0, float(exploration))
        self._random = random.Random(seed)
        key = str(self.path.resolve())
        with _FILE_LOCKS_GUARD:
            self._lock = _FILE_LOCKS.setdefault(key, threading.RLock())
        self._data: dict[str, dict[str, dict[str, float]]] = {}
        self._load()

    @staticmethod
    def context_key(context: str | dict[str, Any]) -> str:
        if isinstance(context, dict):
            return json.dumps(context, sort_keys=True, separators=(",", ":"))
        return str(context).strip()[:500] or "default"

    def _load(self) -> None:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("contexts"), dict):
                self._data = loaded["contexts"]
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            self._data = {}

    def _save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(
                f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            temporary.write_text(
                json.dumps({"version": 1, "contexts": self._data}, sort_keys=True), encoding="utf-8"
            )
            os.replace(temporary, self.path)

    @contextmanager
    def _process_lock(self):
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def choose(self, context: str | dict[str, Any], tools: list[str] | tuple[str, ...]) -> str:
        names = list(dict.fromkeys(str(tool) for tool in tools if str(tool)))
        if not names:
            raise ValueError("tool catalog is empty")
        key = self.context_key(context)
        stats = self._data.setdefault(key, {})
        for name in names:
            if name not in stats:
                return name
        total = sum(max(0.0, float(item.get("count", 0))) for item in stats.values())

        def score(name: str) -> float:
            item = stats[name]
            count = max(1.0, float(item.get("count", 0)))
            mean = float(item.get("reward", 0.0)) / count
            return mean + self.exploration * math.sqrt(math.log(total + 1.0) / count)

        best = max(score(name) for name in names)
        candidates = [name for name in names if abs(score(name) - best) < 1e-12]
        return self._random.choice(candidates)

    def record(self, context: str | dict[str, Any], tool: str, reward: float) -> None:
        with self._lock, self._process_lock():
            self._load()
            key = self.context_key(context)
            item = self._data.setdefault(key, {}).setdefault(
                str(tool), {"count": 0.0, "reward": 0.0}
            )
            item["count"] = min(float(item.get("count", 0.0)) + 1.0, 1_000_000.0)
            item["reward"] = max(
                -1_000_000.0, min(1_000_000.0, float(item.get("reward", 0.0)) + float(reward))
            )
            self._save()

    def snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._data))


ToolCallable = Callable[[dict[str, Any]], Awaitable[Any] | Any]


class AdaptiveToolRouter:
    """Select and execute trusted tools, retrying only bounded transient failures."""

    def __init__(
        self,
        policy: ToolPolicy,
        tools: dict[str, ToolCallable],
        *,
        retries: int = 2,
        backoff: float = 0.05,
        retryable_tools: set[str] | None = None,
    ) -> None:
        self.policy = policy
        self.tools = dict(tools)
        self.retries = max(0, min(int(retries), 5))
        self.backoff = max(0.0, min(float(backoff), 2.0))
        self.retryable_tools = retryable_tools or set()

    async def invoke(
        self,
        context: str | dict[str, Any],
        arguments: dict[str, Any] | None = None,
        *,
        tool: str | None = None,
        candidates: list[str] | None = None,
    ) -> ToolOutcome:
        if tool is not None and tool not in self.tools:
            raise KeyError(f"unknown trusted tool: {tool}")
        if candidates is None:
            if tool is None and len(self.tools) > 1:
                raise ValueError("declare compatible candidates or select an explicit tool")
            available = [tool] if tool else list(self.tools)
        else:
            available = candidates
            if any(name not in self.tools for name in available):
                raise ValueError("candidate is not a trusted tool")
            if tool is not None and tool not in available:
                raise ValueError("selected tool is outside compatible candidates")
        if not available:
            raise ValueError("no compatible trusted tools")
        selected = tool or self.policy.choose(context, available)
        payload = arguments or {}
        attempts = 0
        last_error: Exception | None = None
        for attempts in range(1, self.retries + 2):
            try:
                value = await asyncio.to_thread(self.tools[selected], payload)
                if asyncio.iscoroutine(value):
                    value = await value
                if isinstance(value, dict) and (
                    value.get("success") is False
                    or value.get("isError") is True
                    or value.get("returncode", 0) != 0
                ):
                    raise RuntimeError("tool reported failure")
                if getattr(value, "isError", False):
                    raise RuntimeError("connector reported failure")
                self.policy.record(context, selected, 1.0)
                return ToolOutcome(selected, True, 1.0, attempts, value=value)
            except (OSError, RuntimeError, TimeoutError, ConnectionError) as error:
                last_error = error
                retry_allowed = selected in self.retryable_tools
                if attempts <= self.retries and retry_allowed:
                    await asyncio.sleep(self.backoff * attempts)
                else:
                    break
            except Exception as error:  # tool failures are learned and surfaced
                last_error = error
                break
        self.policy.record(context, selected, -1.0)
        return ToolOutcome(selected, False, -1.0, attempts, error=str(last_error))
