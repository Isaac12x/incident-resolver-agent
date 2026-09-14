from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.config import ExecutionConfig, PermissionsConfig
from src.execution import container_command, execute_container
from src.tools import ToolError, WorkspaceTools


def test_container_mounts_only_workspace_and_drops_privileges(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("src.execution.shutil.which", lambda _: "/bin/docker")
    (tmp_path / ".git").write_text("gitdir: /host/repo/.git")
    (tmp_path / ".github").mkdir()
    command = container_command(
        ["python", "test.py"],
        tmp_path,
        ExecutionConfig(mode="container"),
        PermissionsConfig(),
        "test",
    )
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert command[command.index("--network") + 1] == "none"
    assert not any("docker.sock" in value for value in command)
    assert "target=/workspace/.git,readonly" in " ".join(command)
    assert "target=/workspace/.github,readonly" in " ".join(command)
    restricted = container_command(
        ["true"],
        tmp_path,
        ExecutionConfig(network=True),
        PermissionsConfig(mode="read-only"),
        "test",
    )
    assert f"source={tmp_path},target=/workspace,readonly" in " ".join(restricted)
    assert "bridge" in restricted
    with pytest.raises(ValueError, match="empty"):
        container_command([], tmp_path, ExecutionConfig(), PermissionsConfig(), "test")
    bad = tmp_path / "comma,dir"
    bad.mkdir()
    with pytest.raises(ValueError, match="safe"):
        container_command(["true"], bad, ExecutionConfig(), PermissionsConfig(), "test")
    monkeypatch.setattr("src.execution.shutil.which", lambda _: None)
    with pytest.raises(RuntimeError, match="Docker"):
        container_command(["true"], tmp_path, ExecutionConfig(), PermissionsConfig(), "test")


class Process:
    def __init__(self, *, delayed: bool = False):
        self.returncode = None
        self.delayed = delayed
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stdout.feed_data(b"1234567890")
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    async def wait(self):
        if self.delayed and self.returncode is None:
            await asyncio.sleep(10)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.returncode = -9


@pytest.mark.asyncio
async def test_output_is_bounded_and_container_removed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("src.execution.shutil.which", lambda _: "/bin/docker")
    spawn = AsyncMock(side_effect=[Process(), Process()])
    monkeypatch.setattr("src.execution.asyncio.create_subprocess_exec", spawn)
    result = await execute_container(
        ["test"], tmp_path, ExecutionConfig(), PermissionsConfig(), timeout=1, max_output=8
    )
    assert result == (0, "1234", "", True)
    assert spawn.call_args_list[1].args[1:3] == ("rm", "--force")


@pytest.mark.asyncio
async def test_timeout_removes_container_and_kills_attached_client(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.execution.shutil.which", lambda _: "/bin/docker")
    process = Process(delayed=True)
    spawn = AsyncMock(side_effect=[process, Process()])
    monkeypatch.setattr("src.execution.asyncio.create_subprocess_exec", spawn)
    with pytest.raises(TimeoutError):
        await execute_container(
            ["test"], tmp_path, ExecutionConfig(), PermissionsConfig(), timeout=0.01, max_output=100
        )
    assert process.returncode == -9
    assert spawn.call_count == 2


@pytest.mark.asyncio
async def test_workspace_tool_uses_configured_execution_boundary(tmp_path, monkeypatch) -> None:
    run = AsyncMock(return_value=(1, "", "test failed", False))
    monkeypatch.setattr("src.execution.execute_container", run)
    tool = WorkspaceTools(tmp_path, execution=ExecutionConfig(mode="container"))
    result = await tool.shell("python test.py")
    assert result.returncode == 1
    assert run.call_args.args[0] == ["python", "test.py"]
    run.side_effect = TimeoutError
    with pytest.raises(ToolError, match="container command timed out"):
        await tool.shell("python test.py")
