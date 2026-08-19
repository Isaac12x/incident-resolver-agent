"""Repository graph and structured-tree integrations."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ToolResult:
    """The captured result of one repository-tool command."""

    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class GitHubRepository:
    """A repository returned by the authenticated GitHub CLI account."""

    name: str
    clone_url: str
    base_branch: str = "main"


@dataclass(frozen=True)
class SubscriptionProbeResult:
    """Host subscription CLI availability and optional exec probe results."""

    ready: bool
    message: str
    authenticated: bool = False
    executable: str | None = None


@dataclass(frozen=True)
class RepositorySetupResult:
    """Clone/pull and graph generation results for one managed checkout."""

    path: Path
    acquisition: ToolResult
    code_review_graph: ToolResult | None = None

    @property
    def succeeded(self) -> bool:
        return self.acquisition.succeeded and (
            self.code_review_graph is not None and self.code_review_graph.succeeded
        )


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]

RUNTIME_SEED_SPEC = Path(__file__).with_name("runtime.tree")


def _validate_base(base: Path | str) -> Path:
    path = Path(base).expanduser().resolve()
    if not path.is_dir():
        raise NotADirectoryError(path)
    return path


def _tool_executable(name: str) -> str:
    """Resolve a project CLI next to the running interpreter, then fall back to PATH."""
    import sys

    sibling = Path(sys.executable).resolve().with_name(name)
    if sibling.is_file():
        return str(sibling)
    resolved = shutil.which(name)
    return resolved or name


def _run(command: Sequence[str], cwd: Path, runner: CommandRunner) -> ToolResult:
    try:
        completed = runner(
            list(command),
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        return ToolResult(tuple(command), 127, "", str(error))
    return ToolResult(
        command=tuple(command),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def capture_structured_tree(
    base: Path | str = ".",
    output: Path | str = "structure.seed",
    *,
    runner: CommandRunner = subprocess.run,
) -> ToolResult:
    """Capture ``base`` through seed-cli into a structured tree spec."""
    root = _validate_base(base)
    destination = Path(output).expanduser()
    if not destination.is_absolute():
        destination = (Path.cwd() / destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    return _run(
        (_tool_executable("seed"), "capture", "--base", str(root), "--out", str(destination)),
        cwd=root,
        runner=runner,
    )


def initialise_runtime_tree(
    base: Path | str = ".",
    *,
    spec: Path | str | None = None,
    runner: CommandRunner = subprocess.run,
) -> ToolResult:
    """Create or repair the local ``.agent`` skeleton through seed-cli."""
    root = _validate_base(base)
    project_spec = root / ".seed" / "specs" / "runtime.tree"
    seed_spec = Path(spec).expanduser().resolve() if spec else project_spec
    if not seed_spec.is_file() and spec is None:
        seed_spec = RUNTIME_SEED_SPEC.resolve()
    if not seed_spec.is_file():
        raise FileNotFoundError(seed_spec)
    return _run(
        (
            _tool_executable("seed"),
            "create",
            "--template",
            str(seed_spec),
            "--base",
            str(root),
            "runtime_root=.agent",
        ),
        cwd=root,
        runner=runner,
    )


def build_repository_graphs(
    base: Path | str = ".",
    *,
    runner: CommandRunner = subprocess.run,
) -> ToolResult:
    """Build the code-review-graph index for ``base``."""
    root = _validate_base(base)
    return _run(
        (_tool_executable("code-review-graph"), "build", "--repo", str(root)),
        cwd=root,
        runner=runner,
    )


def subscription_cli_command(command: list[str]) -> list[str]:
    """Return the configured CLI command with Codex's yolo mode enabled."""
    normalized = list(command)
    if normalized and Path(normalized[0]).name == "codex" and "--yolo" not in normalized:
        normalized.insert(1, "--yolo")
    return normalized


def host_subscription_cli_ready(*, runner: CommandRunner = subprocess.run) -> bool:
    """Return whether the default host Codex subscription CLI is authenticated."""
    return probe_subscription_cli(["codex"], runner=runner).ready


def probe_subscription_cli(
    command: list[str],
    profile: str | None = None,
    *,
    runner: CommandRunner = subprocess.run,
    exec_test: bool = False,
    exec_timeout_seconds: int = 120,
) -> SubscriptionProbeResult:
    """Check subscription CLI availability and, optionally, run a minimal exec probe."""
    if not command:
        return SubscriptionProbeResult(False, "subscription CLI command cannot be blank")
    executable = shutil.which(command[0])
    if executable is None and not Path(command[0]).expanduser().is_file():
        return SubscriptionProbeResult(False, f"command not found: {command[0]}")
    resolved = executable or str(Path(command[0]).expanduser())
    normalized = subscription_cli_command(command)
    cli_name = Path(normalized[0]).name
    if cli_name == "codex":
        status_command = [normalized[0], "login", "status"]
        if profile:
            status_command.extend(["--profile", profile])
        status = _run(status_command, Path.cwd(), runner)
        if not status.succeeded:
            message = (status.stderr or status.stdout).strip() or "not authenticated"
            return SubscriptionProbeResult(False, message, executable=resolved)
        auth_message = (status.stdout or status.stderr).strip() or "authenticated"
        if not exec_test:
            return SubscriptionProbeResult(
                True,
                auth_message,
                authenticated=True,
                executable=resolved,
            )
        with tempfile.TemporaryDirectory() as temporary:
            workdir = Path(temporary)
            schema_path = workdir / "schema.json"
            output_path = workdir / "output.json"
            schema_path.write_text(
                (
                    '{"type":"object","properties":{"ok":{"type":"boolean"}},'
                    '"required":["ok"],"additionalProperties":false}'
                ),
                encoding="utf-8",
            )
            exec_command = [*normalized, "exec"]
            if profile:
                exec_command.extend(["--profile", profile])
            exec_command.extend(
                [
                    "--sandbox",
                    "read-only",
                    "--json",
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    "-",
                ]
            )
            try:
                completed = runner(
                    exec_command,
                    cwd=workdir,
                    input='Reply with JSON {"ok": true} only.\n',
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=exec_timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                return SubscriptionProbeResult(
                    False,
                    f"exec probe timed out after {exec_timeout_seconds}s",
                    authenticated=True,
                    executable=resolved,
                )
            if completed.returncode != 0:
                message = (completed.stderr or completed.stdout).strip() or "exec probe failed"
                return SubscriptionProbeResult(
                    False,
                    message,
                    authenticated=True,
                    executable=resolved,
                )
            if not output_path.is_file():
                return SubscriptionProbeResult(
                    False,
                    "exec probe completed without structured output",
                    authenticated=True,
                    executable=resolved,
                )
            try:
                payload = json.loads(output_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return SubscriptionProbeResult(
                    False,
                    "exec probe returned invalid JSON",
                    authenticated=True,
                    executable=resolved,
                )
            if payload.get("ok") is True:
                return SubscriptionProbeResult(
                    True,
                    f"{auth_message}; exec probe succeeded",
                    authenticated=True,
                    executable=resolved,
                )
            return SubscriptionProbeResult(
                False,
                "exec probe returned unexpected output",
                authenticated=True,
                executable=resolved,
            )
    help_result = _run([normalized[0], "--help"], Path.cwd(), runner)
    if help_result.succeeded:
        return SubscriptionProbeResult(True, f"{normalized[0]} is available", executable=resolved)
    message = (help_result.stderr or help_result.stdout).strip() or "command is unavailable"
    return SubscriptionProbeResult(False, message, executable=resolved)


def github_login(*, runner: CommandRunner = subprocess.run) -> ToolResult:
    """Ensure the GitHub CLI is authenticated, opening its web login when needed."""
    cwd = Path.cwd()
    status = _run(("gh", "auth", "status"), cwd, runner)
    if status.succeeded:
        return status
    return _run(("gh", "auth", "login", "--web", "--git-protocol", "https"), cwd, runner)


def list_github_repositories(
    *, runner: CommandRunner = subprocess.run
) -> tuple[ToolResult, list[GitHubRepository]]:
    """List repositories selectable by the currently authenticated GitHub account."""
    result = _run(
        (
            "gh",
            "repo",
            "list",
            "--limit",
            "100",
            "--json",
            "nameWithOwner,url,defaultBranchRef",
        ),
        Path.cwd(),
        runner,
    )
    if not result.succeeded:
        return result, []
    try:
        payload = json.loads(result.stdout)
        repositories = [
            GitHubRepository(
                name=str(item["nameWithOwner"]),
                clone_url=str(item["url"]),
                base_branch=str((item.get("defaultBranchRef") or {}).get("name") or "main"),
            )
            for item in payload
        ]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        return ToolResult(result.command, 1, result.stdout, f"invalid GitHub response: {error}"), []
    return result, sorted(repositories, key=lambda repository: repository.name.casefold())


def repository_name_from_url(clone_url: str) -> str:
    """Derive an owner/name identifier from HTTPS, SSH, or local clone URLs."""
    value = clone_url.strip().rstrip("/")
    path = value.split(":", 1)[1] if value.startswith("git@") and ":" in value else value
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        raise ValueError("clone URL must identify an owner and repository")
    return "/".join(parts[-2:]).removesuffix(".git")


def repository_slug(name: str) -> str:
    """Return the safe managed-checkout directory for an owner/name identifier."""
    normalized = name.strip().strip("/")
    parts = normalized.split("/")
    if (
        not normalized
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", normalized)
        or any(part in {".", ".."} for part in parts)
    ):
        raise ValueError("repository name must use the owner/name format")
    return normalized.replace("/", "--")


def clone_and_index_repository(
    clone_url: str,
    destination: Path | str,
    *,
    runner: CommandRunner = subprocess.run,
) -> RepositorySetupResult:
    """Clone or fast-forward a managed checkout, then generate the repository graph."""
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not (target / ".git").exists():
            raise FileExistsError(f"repository destination is not a Git checkout: {target}")
        acquisition = _run(("git", "-C", str(target), "pull", "--ff-only"), target.parent, runner)
    else:
        acquisition = _run(("git", "clone", clone_url, str(target)), target.parent, runner)
    if not acquisition.succeeded:
        return RepositorySetupResult(target, acquisition)
    code_review_graph = build_repository_graphs(target, runner=runner)
    return RepositorySetupResult(target, acquisition, code_review_graph)
