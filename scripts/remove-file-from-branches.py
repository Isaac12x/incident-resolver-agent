#!/usr/bin/env python3
"""Remove one tracked file from one or more branches and update their PRs.

The script works in temporary detached worktrees, so it never checks out or
modifies the caller's current branch.  It is dry-run by default; pass
``--apply`` to commit and push the changes.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def command(args: list[str], *, cwd: Path, capture: bool = True) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=capture,
    )
    return result.stdout.strip() if capture else ""


def relative_file(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise argparse.ArgumentTypeError("file must be a relative path inside the repository")
    if not path.parts:
        raise argparse.ArgumentTypeError("file must not be empty")
    return path


def pull_requests(repo: str, branch: str, *, cwd: Path) -> list[dict[str, Any]]:
    output = command(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number,url,title",
        ],
        cwd=cwd,
    )
    return json.loads(output or "[]")


def push_command(branch: str) -> list[str]:
    """Build the push command, allowing the history rewrite required by master."""
    force = ["--force"] if branch == "master" else []
    return ["git", "push", *force, "origin", f"HEAD:refs/heads/{branch}"]


def process_branch(
    repository: Path,
    repo: str,
    branch: str,
    file: Path,
    *,
    apply: bool,
) -> bool:
    prs = pull_requests(repo, branch, cwd=repository)
    labels = ", ".join(f"#{pr['number']}" for pr in prs) or "no open PR"
    print(f"{branch}: {labels}")

    remote_ref = f"refs/remotes/origin/{branch}"
    command(["git", "fetch", "origin", branch], cwd=repository, capture=False)
    with tempfile.TemporaryDirectory(prefix="remove-file-") as temporary:
        worktree = Path(temporary)
        command(["git", "worktree", "add", "--detach", str(worktree), remote_ref], cwd=repository)
        try:
            target = worktree / file
            if not target.exists() and not target.is_symlink():
                print(f"  {file}: not present; nothing to do")
                return False
            if target.is_dir() and not target.is_symlink():
                raise RuntimeError(f"{file} is a directory; refusing to remove it")

            if not apply:
                print(f"  would remove {file}, commit, and push {branch}")
                return True

            target.unlink()
            command(["git", "add", "--", str(file)], cwd=worktree)
            command(
                ["git", "commit", "-m", f"chore: remove {file.as_posix()}"],
                cwd=worktree,
                capture=False,
            )
            command(push_command(branch), cwd=worktree, capture=False)
            print(f"  removed {file}, committed, and pushed; PR(s) update automatically")
            return True
        finally:
            command(["git", "worktree", "remove", "--force", str(worktree)], cwd=repository)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=relative_file, help="relative path to remove")
    parser.add_argument("branches", nargs="+", help="one or more branch names")
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path.cwd(),
        help="local checkout to use (default: current directory)",
    )
    parser.add_argument(
        "--repo", help="GitHub OWNER/REPO (default: gh infers it from the checkout)"
    )
    parser.add_argument(
        "--apply", action="store_true", help="commit and push; otherwise show a dry run"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    repository = args.repository.resolve()
    if not (repository / ".git").exists() and not (repository / ".git").is_file():
        print(f"not a Git checkout: {repository}", file=sys.stderr)
        return 2

    if not shutil.which("gh"):
        print("gh is required; install GitHub CLI and run gh auth login", file=sys.stderr)
        return 2
    repo = args.repo or command(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        cwd=repository,
    )

    try:
        for branch in dict.fromkeys(args.branches):
            process_branch(repository, repo, branch, args.file, apply=args.apply)
    except (OSError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
