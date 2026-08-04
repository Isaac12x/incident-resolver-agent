"""Constrained local coding tools used by the incident agent."""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .config import PermissionsConfig


class ToolError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    command: str
    returncode: int
    stdout: str
    stderr: str
    truncated: bool = False


class WorkspaceTools:
    def __init__(
        self,
        workspace: Path | str,
        *,
        timeout: int = 600,
        max_output: int = 100_000,
        logger: Callable[[dict[str, object]], None] | None = None,
        permissions: PermissionsConfig | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.timeout = timeout
        self.max_output = max_output
        self.logger = logger
        self.permissions = permissions or PermissionsConfig()

    def _path(self, relative_path: str) -> Path:
        if not relative_path or Path(relative_path).is_absolute():
            raise ToolError("path must be relative to the workspace")
        path = (self.workspace / relative_path).resolve()
        if not path.is_relative_to(self.workspace):
            raise ToolError("path escapes the workspace")
        if any(part in {".git", ".agent"} for part in Path(relative_path).parts):
            raise ToolError("direct access to control directories is forbidden")
        return path

    def read_file(self, relative_path: str) -> str:
        return self._path(relative_path).read_text(encoding="utf-8")

    def write_file(self, relative_path: str, content: str) -> None:
        self._validate_write(relative_path)
        path = self._path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def replace_in_file(self, relative_path: str, old: str, new: str) -> None:
        self._validate_write(relative_path)
        path = self._path(relative_path)
        content = path.read_text(encoding="utf-8")
        occurrences = content.count(old)
        if occurrences != 1:
            raise ToolError(f"expected exactly one match, found {occurrences}")
        path.write_text(content.replace(old, new, 1), encoding="utf-8")

    def _validate_write(self, relative_path: str) -> None:
        if self.permissions.mode == "read-only":
            raise ToolError("write operations are disabled in read-only mode")
        parts = {part.casefold() for part in Path(relative_path).parts}
        if not self.permissions.allow_ci_modification and (
            ".github" in parts
            or ".circleci" in parts
            or ".gitlab-ci.yml" in parts
            or "azure-pipelines.yml" in parts
            or "circle.yml" in parts
            or "jenkinsfile" in parts
        ):
            raise ToolError("CI modification is disabled")
        if not self.permissions.allow_snapshot_updates and (
            "__snapshots__" in parts or any(part.endswith((".snap", ".snapshot")) for part in parts)
        ):
            raise ToolError("snapshot updates are disabled")
        if not self.permissions.allow_migrations and any(
            part in {"migration", "migrations"} for part in parts
        ):
            raise ToolError("migration changes are disabled")

    @staticmethod
    def _secret_environment() -> dict[str, str]:
        secret_markers = (
            "TOKEN",
            "SECRET",
            "PASSWORD",
            "PASSWD",
            "PRIVATE_KEY",
            "API_KEY",
            "ACCESS_KEY",
            "CREDENTIAL",
        )
        return {
            key: value
            for key, value in os.environ.items()
            if not any(marker in key.upper() for marker in secret_markers)
            and not key.upper().endswith("_KEY")
        }

    def _validate_command(self, command: str) -> list[str]:
        try:
            tokens = shlex.split(command)
        except ValueError as error:
            raise ToolError(f"invalid command: {error}") from error
        if not tokens:
            raise ToolError("command cannot be empty")

        operators = {"|", "||", "&", "&&", ";", ">", ">>", "<", "<<"}
        if any(
            token in operators or token.startswith((">", "<")) or "`" in token or "$(" in token
            for token in tokens
        ):
            raise ToolError("shell operators and command substitution are forbidden")

        executable = Path(tokens[0]).name.casefold()
        forbidden = {"sudo", "su", "mount", "umount", "shutdown", "reboot"}
        if executable in forbidden:
            raise ToolError(f"forbidden command: {executable}")
        inline_interpreter = executable.startswith("python") or executable in {
            "sh",
            "bash",
            "zsh",
            "fish",
            "node",
            "ruby",
            "perl",
        }
        if inline_interpreter and any(token in {"-c", "-e", "--eval"} for token in tokens[1:]):
            raise ToolError("inline interpreter and shell programs are forbidden")

        destructive_flags = {"-rf", "-fr", "--no-preserve-root"}
        if executable == "rm" and any(token in destructive_flags for token in tokens):
            raise ToolError("recursive forced deletion is forbidden")

        for index, token in enumerate(tokens):
            if "../" in token or "..\\" in token or token in {"..", "~"}:
                raise ToolError("command path escapes the workspace")
            if index == 0 or token.startswith(("http://", "https://")):
                continue
            candidate_value = token.split("=", 1)[-1] if "=" in token else token
            if candidate_value.startswith("~"):
                raise ToolError("command path escapes the workspace")
            candidate = Path(candidate_value)
            if candidate.is_absolute() and not candidate.resolve().is_relative_to(self.workspace):
                raise ToolError("absolute command paths must stay inside the workspace")
            if any(part in {".git", ".agent"} for part in candidate.parts):
                raise ToolError("direct access to control directories is forbidden")

        self._validate_command_permissions(tokens, executable)
        return tokens

    def _validate_command_permissions(self, tokens: list[str], executable: str) -> None:
        arguments = [token.casefold() for token in tokens[1:]]
        argument_parts = {
            part.casefold()
            for argument in arguments
            if not argument.startswith("-")
            for part in Path(argument.split("=", 1)[-1]).parts
        }
        if not self.permissions.allow_ci_modification and argument_parts.intersection(
            {
                ".circleci",
                ".github",
                ".gitlab-ci.yml",
                "azure-pipelines.yml",
                "circle.yml",
                "jenkinsfile",
            }
        ):
            raise ToolError("CI modification through shell commands is disabled")
        if not self.permissions.allow_snapshot_updates and (
            "__snapshots__" in argument_parts
            or any(part.endswith((".snap", ".snapshot")) for part in argument_parts)
            or any("snapshot" in argument and "update" in argument for argument in arguments)
        ):
            raise ToolError("snapshot updates through shell commands are disabled")
        if self.permissions.mode == "read-only":
            mutating_commands = {
                "chmod",
                "chown",
                "cp",
                "install",
                "ln",
                "mkdir",
                "mv",
                "rm",
                "tee",
                "touch",
                "truncate",
            }
            mutating_git = {
                "add",
                "am",
                "apply",
                "branch",
                "checkout",
                "cherry-pick",
                "clean",
                "commit",
                "merge",
                "mv",
                "pull",
                "push",
                "rebase",
                "reset",
                "restore",
                "revert",
                "rm",
                "stash",
                "switch",
                "tag",
            }
            if executable in mutating_commands:
                raise ToolError("mutating shell commands are disabled in read-only mode")
            if executable == "git" and any(argument in mutating_git for argument in arguments):
                raise ToolError("mutating Git commands are disabled in read-only mode")
            if "-i" in arguments or "--in-place" in arguments or "--fix" in arguments:
                raise ToolError("in-place command changes are disabled in read-only mode")

        install_subcommands = {
            "add",
            "get",
            "install",
            "lock",
            "sync",
            "update",
            "upgrade",
        }
        package_managers = {
            "bun",
            "cargo",
            "go",
            "npm",
            "pip",
            "pip3",
            "pnpm",
            "poetry",
            "uv",
            "yarn",
        }
        if (
            not self.permissions.allow_dependency_installation
            and executable in package_managers
            and any(argument in install_subcommands for argument in arguments)
        ):
            raise ToolError("dependency installation is disabled")

        migration_tools = {"alembic", "django-admin", "prisma"}
        runs_manage_py = executable in {"python", "python3"} and any(
            Path(argument).name == "manage.py" for argument in arguments
        )
        if (
            not self.permissions.allow_migrations
            and (executable in migration_tools or runs_manage_py)
            and any(argument in {"migrate", "migration", "upgrade"} for argument in arguments)
        ):
            raise ToolError("database migrations are disabled")

    async def shell(self, command: str) -> CommandResult:
        tokens = self._validate_command(command)
        try:
            process = await asyncio.create_subprocess_exec(
                *tokens,
                cwd=self.workspace,
                env=self._secret_environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
        except OSError as error:
            result = CommandResult(command, 127, "", str(error))
            self._log(result)
            return result
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout
            )
        except TimeoutError as error:
            if os.name == "posix":
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            await process.wait()
            raise ToolError(f"command timed out after {self.timeout}s") from error
        stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace")
        truncated = len(stdout) + len(stderr) > self.max_output
        if truncated:
            allowance = self.max_output // 2
            stdout, stderr = stdout[:allowance], stderr[:allowance]
        result = CommandResult(command, process.returncode or 0, stdout, stderr, truncated)
        self._log(result)
        return result

    def _log(self, result: CommandResult) -> None:
        if self.logger:
            self.logger(
                {
                    "type": "tool.shell",
                    "command": result.command,
                    "returncode": result.returncode,
                    "truncated": result.truncated,
                }
            )
