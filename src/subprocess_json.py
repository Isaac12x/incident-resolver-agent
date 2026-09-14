"""Bounded JSON subprocess execution for optional local providers."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from typing import Any


def run_bounded_json(
    command: list[str],
    payload: object,
    *,
    timeout_seconds: int = 20,
    max_bytes: int = 64 * 1024,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run an argv provider with JSON stdin and bounded stdout/deadline."""
    if not command:
        raise ValueError("command is required")
    if not 1 <= timeout_seconds <= 120 or not 256 <= max_bytes <= 64 * 1024:
        raise ValueError("invalid subprocess bounds")
    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        deadline = time.monotonic() + timeout_seconds
        if process.stdin:
            input_bytes = json.dumps(payload).encode() + b"\n"
            os.set_blocking(process.stdin.fileno(), False)
            offset = 0
            while offset < len(input_bytes):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout_seconds)
                # Use short nonblocking writes to avoid deadlocking on a full pipe.
                try:
                    offset += os.write(process.stdin.fileno(), input_bytes[offset : offset + 4096])
                except BlockingIOError:
                    time.sleep(min(0.01, remaining))
            process.stdin.close()
        selector = selectors.DefaultSelector()
        if process.stdout:
            selector.register(process.stdout, selectors.EVENT_READ)
        chunks: list[bytes] = []
        total = 0
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout_seconds)
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, min(4096, max_bytes + 1 - total))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("provider output exceeded limit")
        selector.close()
        process.wait(timeout=max(0.1, deadline - time.monotonic()))
        try:
            value = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"available": False, "reason": "provider returned invalid JSON"}
        if not isinstance(value, dict):
            return {"available": False, "reason": "provider JSON must be an object"}
        return {
            "available": process.returncode == 0,
            "result": value,
            "returncode": process.returncode,
        }
    except ValueError as error:
        return {"available": False, "reason": str(error)}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"available": False, "reason": type(error).__name__}
    finally:
        if selector is not None:
            selector.close()
        if process is not None:
            for stream in (process.stdin, process.stdout):
                if stream is not None and not stream.closed:
                    stream.close()
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=2)
