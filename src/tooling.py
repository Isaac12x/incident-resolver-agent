"""Repository graph and structured-tree integrations."""

from __future__ import annotations

import json
import re
import subprocess
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
class RepositorySetupResult:
    """Clone/pull and graph generation results for one managed checkout."""

    path: Path
    acquisition: ToolResult
    graphify: ToolResult | None = None
    code_review_graph: ToolResult | None = None

    @property
    def succeeded(self) -> bool:
        return self.acquisition.succeeded and all(
            result is not None and result.succeeded
            for result in (self.graphify, self.code_review_graph)
        )


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _validate_base(base: Path | str) -> Path:
    path = Path(base).expanduser().resolve()
    if not path.is_dir():
        raise NotADirectoryError(path)
    return path


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
        ("seed", "capture", "--base", str(root), "--out", str(destination)),
        cwd=root,
        runner=runner,
    )


def build_repository_graphs(
    base: Path | str = ".",
    *,
    runner: CommandRunner = subprocess.run,
) -> tuple[ToolResult, ToolResult]:
    """Build graphify and code-review-graph indexes for ``base``."""
    root = _validate_base(base)
    graphify = _run(
        ("graphify", "extract", str(root), "--code-only", "--no-cluster"),
        cwd=root,
        runner=runner,
    )
    code_review_graph = _run(
        ("code-review-graph", "build", "--repo", str(root)),
        cwd=root,
        runner=runner,
    )
    return graphify, code_review_graph


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
    """Clone or fast-forward a managed checkout, then generate both repository graphs."""
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
    graphify, code_review_graph = build_repository_graphs(target, runner=runner)
    return RepositorySetupResult(target, acquisition, graphify, code_review_graph)
