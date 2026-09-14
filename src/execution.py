"""Bounded container execution for untrusted repository commands.

Only the incident worktree is mounted; host credentials and the container engine
socket are never forwarded. Missing isolation prerequisites fail closed.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from .config import ExecutionConfig, PermissionsConfig


def container_command(
    tokens: list[str],
    workspace: Path,
    policy: ExecutionConfig,
    permissions: PermissionsConfig,
    name: str,
) -> list[str]:
    executable = shutil.which("docker")
    if executable is None:
        raise RuntimeError("container execution requires Docker; run incident-agent doctor")
    if not tokens:
        raise ValueError("container command cannot be empty")
    root = workspace.resolve(strict=True)
    if not root.is_dir() or any(c in str(root) for c in ",\n\r"):
        raise ValueError("workspace cannot be represented as a safe container mount")
    mount = f"type=bind,source={root},target=/workspace"
    if permissions.mode == "read-only":
        mount += ",readonly"
    command = [
        executable,
        "run",
        "--rm",
        "--name",
        name,
        "--pull=never",
        "--label",
        "incident-harness.managed=true",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit",
        str(policy.pids_limit),
        "--memory",
        f"{policy.memory_mb}m",
        "--network",
        "bridge" if policy.network else "none",
        "--tmpfs",
        "/tmp:rw,nosuid,size=64m",
        "--workdir",
        "/workspace",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--mount",
        mount,
    ]
    # Protect existing harness/Git/CI control files even when source edits are enabled.
    protected = [".git", ".agent"]
    if not permissions.allow_ci_modification:
        protected.append(".github")
    for relative in protected:
        path = root / relative
        if path.exists() and not path.is_symlink():
            command.extend(
                ["--mount", f"type=bind,source={path},target=/workspace/{relative},readonly"]
            )
    return [*command, policy.image, *tokens]


async def execute_container(
    tokens: list[str],
    workspace: Path,
    policy: ExecutionConfig,
    permissions: PermissionsConfig,
    *,
    timeout: float,
    max_output: int,
) -> tuple[int, str, str, bool]:
    name = "incident-command-" + uuid4().hex
    command = container_command(tokens, workspace, policy, permissions, name)
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def capture(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
        saved = bytearray()
        truncated = False
        while chunk := await stream.read(65536):
            remaining = max(0, max_output // 2 - len(saved))
            saved.extend(chunk[:remaining])
            truncated |= len(chunk) > remaining
        return bytes(saved), truncated

    readers = [asyncio.create_task(capture(stream)) for stream in (process.stdout, process.stderr)]
    try:
        async with asyncio.timeout(timeout):
            await process.wait()
            stdout, stderr = await asyncio.gather(*readers)
        return (
            process.returncode,
            stdout[0].decode(errors="replace"),
            stderr[0].decode(errors="replace"),
            stdout[1] or stderr[1],
        )
    finally:
        # Killing the attached client alone does not reliably kill its container.
        cleanup = await asyncio.create_subprocess_exec(
            command[0],
            "rm",
            "--force",
            name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(cleanup.wait(), timeout=10)
        except TimeoutError:
            cleanup.kill()
            await cleanup.wait()
        if process.returncode is None:
            process.kill()
            await process.wait()
        for reader in readers:
            reader.cancel()
        for reader in readers:
            with suppress(asyncio.CancelledError):
                await reader
