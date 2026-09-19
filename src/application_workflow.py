"""Application-scoped lifecycle helpers.

The ordinary workflow deliberately remains repository oriented.  This module is
the narrow adapter used when a task contains a snapshotted application scope:
each repository is represented by a small state object and effects are committed
after every repository so a process crash cannot duplicate earlier publication.
The helpers use attribute based access for compatibility with task snapshots
written by older harness versions.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from .models import (
    DeploymentReference,
    PullRequestReference,
    TaskEvent,
    TaskRecord,
    TaskState,
)
from .tools import WorkspaceTools
from .verification_graph import VerificationGraph


def _states(task: TaskRecord) -> dict[str, Any]:
    return task.repositories or {task.repository: task.repository_state()}


def _state(task: TaskRecord, repository: str) -> Any:
    states = _states(task)
    if task.repositories:
        state = next(
            (value for name, value in states.items() if name.casefold() == repository.casefold()),
            None,
        )
        if state is None:
            raise KeyError(f"repository is not in task scope: {repository}")
        return state
    return states.get(repository)


def _value(task: TaskRecord, repository: str, name: str, default: Any = None) -> Any:
    state = _state(task, repository)
    return getattr(state, name, default)


def _update_state(task: TaskRecord, repository: str, **updates: Any) -> None:
    if task.repositories:
        canonical = next(
            (name for name in task.repositories if name.casefold() == repository.casefold()),
            None,
        )
        if canonical is None:
            raise KeyError(f"repository is not in task scope: {repository}")
        state = task.repositories[canonical]
    else:
        state = None
    if state is None:
        for name, value in updates.items():
            if name in type(task).model_fields:
                setattr(task, name, value)
        return
    for name, value in updates.items():
        setattr(state, name, value)


def _view(task: TaskRecord, repository: str) -> TaskRecord:
    """Build a repository-local view for existing verifier/GitHub contracts."""
    if not task.repositories:
        return task
    updates = {"repository": repository}
    for name in (
        "branch",
        "pr_number",
        "pr_url",
        "pr_head_sha",
        "deployment_environment",
        "deployment_sha",
        "deployment_url",
        "playwright_status",
        "code_review_sha",
        "pending_review_comments",
        "conflict_recovery_attempts",
        "conflict_base_branch",
        "conflict_pending",
        "conflict_merge_pending",
    ):
        updates[name] = _value(task, repository, name, getattr(task, name, None))
    return task.model_copy(update=updates)


def _repository_names(task: TaskRecord) -> list[str]:
    return list(_states(task))


def _git(worktree: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(worktree), *args], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed")
    return result.stdout.strip()


def _commit(worktree: Path, message: str) -> str:
    subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "add",
            "-A",
            "--",
            ".",
            ":(exclude)harness-out",
            ":(exclude).code-review-graph",
            ":(exclude).code-review-graph.db",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "-c",
            "user.name=Incident Agent",
            "-c",
            "user.email=incident-agent@localhost",
            "commit",
            "--allow-empty",
            "-m",
            message,
        ],
        check=True,
    )
    return _git(worktree, "rev-parse", "HEAD")


class ApplicationWorkflow:
    """Coordinate publication and verification for one application task."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine

    @property
    def storage(self) -> Any:
        return self.engine.storage

    @property
    def config(self) -> Any:
        return self.engine.config

    def repositories(self, task: TaskRecord) -> list[str]:
        return _repository_names(task)

    def is_application(self, task: TaskRecord) -> bool:
        return bool(getattr(task, "application", None) and getattr(task, "repositories", None))

    def worktree(self, task: TaskRecord, repository: str) -> Path:
        helper = getattr(self.storage, "repository_worktree", None)
        if helper:
            return Path(helper(task, repository))
        return self.storage.root / "worktrees" / task.task_id / repository.replace("/", "--")

    def verify_workspaces(self, task: TaskRecord) -> None:
        for repository in self.repositories(task):
            worktree = self.worktree(task, repository)
            if not worktree.is_dir():
                raise FileNotFoundError(worktree)
            self.storage.catalog.verify_workspace(task.task_id, worktree, repository)

    def view(self, task: TaskRecord, repository: str) -> TaskRecord:
        return _view(task, repository)

    def revision_set(self, task: TaskRecord) -> dict[str, Any]:
        return {
            repository: str(_value(task, repository, "pr_head_sha", ""))
            for repository in self.repositories(task)
            if _value(task, repository, "changed", True) and _value(task, repository, "pr_head_sha")
        }

    def integration_revision(self, task: TaskRecord) -> str:
        application = self.config.application(getattr(task, "application", ""))
        revisions = dict(self.revision_set(task))
        for repository in self.repositories(task):
            try:
                worktree = self.worktree(task, repository)
                revisions[repository] = {
                    "revision": revisions.get(repository, ""),
                    "head": _git(worktree, "rev-parse", "HEAD"),
                    "files": VerificationGraph(
                        worktree,
                        self.storage.task_directory(task.task_id)
                        / "artifacts/integration-input.json",
                    ).files(),
                }
            except (RuntimeError, OSError, subprocess.SubprocessError) as error:
                raise RuntimeError(
                    f"cannot establish current integration inputs for {repository}"
                ) from error
        payload = json.dumps(
            {"command": getattr(application, "integration_command", ""), "repositories": revisions},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def _dirty(self, worktree: Path) -> bool:
        return bool(
            _git(
                worktree,
                "status",
                "--porcelain",
                "--",
                ".",
                ":(exclude)harness-out",
                ":(exclude).code-review-graph",
                ":(exclude).code-review-graph.db",
            )
        )

    @staticmethod
    def verification_pending(ledger: VerificationGraph) -> list[str]:
        """Return checks without current evidence, allowing broader reruns to supersede them."""
        runs = ledger.data["runs"]
        pending: list[str] = []
        for index, run in enumerate(runs):
            if run["passed"] and run["inputs"] == ledger.snapshot(run["paths"], run["command"]):
                continue
            # A later successful scoped run containing this run's scope is a
            # durable replacement for the narrower evidence.
            scope = set(run["paths"])
            superseded = any(
                later["command"] == run["command"]
                and later["passed"]
                and (scope <= set(later["paths"]) or set(run["inputs"]) <= set(later["inputs"]))
                and later["inputs"] == ledger.snapshot(later["paths"], later["command"])
                for later in runs[index + 1 :]
            )
            if not superseded:
                pending.append(run["command"])
        return pending

    @staticmethod
    def verification_plan(ledger: VerificationGraph, responsibility: list[str]) -> dict[str, Any]:
        """Plan rings from completed scoped runs even when a graph refresh is pending."""
        plan = ledger.plan([], responsibility)
        covered = {path for run in ledger.data["runs"] if run["passed"] for path in run["paths"]}
        next_ring = next(
            (index for index, ring in enumerate(plan["rings"]) if not set(ring) <= covered),
            None,
        )
        pending = [sorted(set(ring) - covered) for ring in plan["rings"]]
        return {
            **plan,
            "phase": "complete"
            if next_ring is None
            else ("fix_incident" if next_ring == 0 else "expand"),
            "pending": pending,
            "next_ring": next_ring,
            "next_paths": pending[next_ring] if next_ring is not None else [],
        }

    def _verification_current(self, task: TaskRecord, repository: str) -> bool:
        state = _state(task, repository)
        command = getattr(state, "verification_command", None)
        if getattr(state, "verification_status", None) != "passed" or not command:
            return False
        ledger = VerificationGraph(
            self.worktree(task, repository),
            self.storage.task_directory(task.task_id)
            / f"artifacts/local/{repository.replace('/', '--')}/verification-graph.json",
        )
        if self.verification_pending(ledger):
            return False
        if ledger.data["seeds"]:
            configured = self.config.repository(repository)
            if (
                self.verification_plan(ledger, configured.responsibility_paths)["next_ring"]
                is not None
            ):
                return False
        for run in reversed(ledger.data["runs"]):
            if run["command"] != command or not run["passed"]:
                continue
            inputs = ledger.snapshot(run["paths"], command)
            return run["inputs"] == inputs
        return False

    def _base_sha(self, task: TaskRecord, repository: str) -> str | None:
        state = _state(task, repository)
        for name in ("base_sha", "base_commit"):
            value = getattr(state, name, None)
            if value:
                return str(value)
        try:
            configured = self.config.repository(repository).base_branch
            return _git(self.worktree(task, repository), "merge-base", "HEAD", configured)
        except (RuntimeError, OSError):
            return None

    def changed_repositories(self, task: TaskRecord) -> list[str]:
        changed: list[str] = []
        for repository in self.repositories(task):
            state = _state(task, repository)
            if _value(task, repository, "changed", False) or _value(task, repository, "pr_number"):
                changed.append(repository)
                continue
            worktree = self.worktree(task, repository)
            base = self._base_sha(task, repository)
            if worktree.exists() and (
                self._dirty(worktree)
                or (base and _git(worktree, "diff", "--name-only", f"{base}..HEAD"))
            ):
                changed.append(repository)
                if state is not None:
                    state.changed = True
        return changed

    def requires_remote_deployment(self, repository: str) -> bool:
        configured = self.config.repository(repository)
        return not (
            configured.publish_mode == "local"
            or (configured.publish_mode == "auto" and self.engine.github.api is None)
        )

    def _persist(self, task: TaskRecord) -> TaskRecord:
        self.storage.save_task(task)
        return self.storage.load_task(task.task_id)

    def _write_group_artifact(self, task: TaskRecord) -> None:
        members = []
        for repository in self.repositories(task):
            members.append(
                {
                    "repository": repository,
                    "branch": _value(task, repository, "branch"),
                    "pr_number": _value(task, repository, "pr_number"),
                    "pr_url": _value(task, repository, "pr_url"),
                    "head_sha": _value(task, repository, "pr_head_sha"),
                    "deployment_sha": _value(task, repository, "deployment_sha"),
                    "verification": _value(task, repository, "playwright_status")
                    or _value(task, repository, "verification_status"),
                }
            )
        self.storage.write_artifact(
            task.task_id,
            "artifacts/application-publication.json",
            json.dumps(
                {
                    "application": getattr(task, "application", None),
                    "external_id": task.external_id,
                    "repositories": members,
                },
                indent=2,
            ),
        )

    async def publish(self, task: TaskRecord) -> TaskRecord:
        """Publish changed repositories, checkpointing after each successful target."""
        self.verify_workspaces(task)
        changed = self.changed_repositories(task)
        if not changed:
            raise RuntimeError("application fix did not change any configured repository")
        for repository in changed:
            current = self.storage.load_task(task.task_id)
            local = self.view(current, repository)
            worktree = self.worktree(current, repository)
            if self.config.code_review.enabled and not await self.engine._review_fix(
                local, worktree
            ):
                return self.storage.load_task(task.task_id)
            existing_sha = _value(current, repository, "pr_head_sha")
            head = _git(worktree, "rev-parse", "HEAD") if worktree.exists() else ""
            already_published = bool(
                existing_sha and head == existing_sha and not self._dirty(worktree)
            )
            if not already_published and not self._verification_current(current, repository):
                raise RuntimeError(
                    f"local verification is stale or missing for {repository}; rerun its checks"
                )
            if already_published:
                continue
            repo_config = self.config.repository(repository)
            if repo_config.publish_mode == "local" or (
                repo_config.publish_mode == "auto" and self.engine.github.api is None
            ):
                sha = _commit(worktree, f"Fix incident {current.external_id}: {current.summary}")
                reference = PullRequestReference(
                    repository=repository,
                    number=0,
                    url=f"local://{current.task_id}/{repository}",
                    head_sha=sha,
                    branch=_value(current, repository, "branch", ""),
                )
                local_verification = {
                    "verification_sha": sha,
                    "verification_status": "passed",
                }
            else:
                reference = await self.engine._publish(
                    local, worktree, update=bool(_value(current, repository, "pr_number"))
                )
                local_verification = {}
            current = self.storage.load_task(current.task_id)
            sha_changed = existing_sha != reference.head_sha
            invalidated = (
                {
                    "deployment_environment": None,
                    "deployment_sha": None,
                    "deployment_url": None,
                    "playwright_status": None,
                    "merged": False,
                    "conflict_pending": False,
                    "conflict_merge_pending": False,
                    "conflict_base_branch": None,
                }
                if sha_changed
                else {}
            )
            if (
                sha_changed
                and _value(current, repository, "verification_sha") != reference.head_sha
            ):
                invalidated.update(
                    verification_sha=None,
                    verification_status=None,
                    verification_command=None,
                    verification_output="",
                )
            if sha_changed and _value(current, repository, "code_review_sha") != reference.head_sha:
                invalidated["code_review_sha"] = None
            updates = {**invalidated, **local_verification}
            _update_state(
                current,
                repository,
                changed=True,
                branch=reference.branch,
                pr_number=reference.number,
                pr_url=reference.url,
                pr_head_sha=reference.head_sha,
                **updates,
            )
            self.storage.append_event(
                current.task_id,
                TaskEvent(
                    type="publication.repository",
                    data={
                        "repository": repository,
                        "sha": reference.head_sha,
                        "pr_number": reference.number,
                    },
                ),
            )
            self._persist(current)
            self._write_group_artifact(current)
        result = self.storage.load_task(task.task_id)
        sync = getattr(self.engine.github, "sync_application_pull_requests", None)
        if sync and result.repositories:
            await sync(result)
        return self.storage.load_task(task.task_id)

    def accept_deployment(
        self, task: TaskRecord, repository: str, deployment: DeploymentReference
    ) -> bool:
        local = self.view(task, repository)
        return bool(self.engine.verifier and self.engine.verifier.accepts(local, deployment))

    def record_deployment(
        self, task: TaskRecord, repository: str, deployment: DeploymentReference
    ) -> TaskRecord:
        _update_state(
            task,
            repository,
            deployment_environment=deployment.environment,
            deployment_sha=deployment.sha,
            deployment_url=deployment.url,
        )
        self.storage.append_event(
            task.task_id,
            TaskEvent(
                type="deployment.repository",
                data={"repository": repository, **deployment.model_dump(mode="json")},
            ),
        )
        return self._persist(task)

    def all_deployments_verified(self, task: TaskRecord) -> bool:
        changed = self.changed_repositories(task)
        try:
            self.verify_workspaces(task)
        except (FileNotFoundError, KeyError, RuntimeError, OSError):
            return False

        def current_revision(repository: str) -> bool:
            expected = _value(task, repository, "pr_head_sha")
            worktree = self.worktree(task, repository)
            if not expected or not worktree.is_dir():
                return False
            try:
                actual = _git(worktree, "rev-parse", "HEAD")
                return actual == expected and not self._dirty(worktree)
            except (RuntimeError, OSError):
                return False

        return bool(changed) and all(
            (
                (
                    current_revision(repository)
                    and _value(task, repository, "verification_status") == "passed"
                    and _value(task, repository, "verification_sha")
                    == _value(task, repository, "pr_head_sha")
                )
                if not self.requires_remote_deployment(repository)
                else (
                    current_revision(repository)
                    and _value(task, repository, "playwright_status") == "passed"
                    and _value(task, repository, "pr_head_sha")
                    == _value(task, repository, "deployment_sha")
                )
            )
            for repository in changed
        )

    async def run_integration(self, task: TaskRecord) -> bool:
        self.verify_workspaces(task)
        application = self.config.application(getattr(task, "application", ""))
        command = getattr(application, "integration_command", "")
        if not command:
            return True
        revision = self.integration_revision(task)
        artifact = self.storage.task_directory(task.task_id) / "artifacts/integration.json"
        if artifact.exists():
            try:
                cached = json.loads(artifact.read_text())
                if cached.get("revision") == revision and cached.get("passed") is True:
                    return True
            except (OSError, ValueError):
                pass
        roots = {
            repository: self.worktree(task, repository) for repository in self.repositories(task)
        }
        parent = self.storage.root / "worktrees" / task.task_id
        parent.mkdir(parents=True, exist_ok=True)
        tools = WorkspaceTools(
            roots,
            repositories=roots,
            parent_workspace=parent,
            default_repository=self.repositories(task)[0],
            timeout=self.config.model.tool_timeout_seconds,
            permissions=self.config.permissions,
            execution=self.config.execution,
        )
        result = await tools.shell(command, integration=True)
        passed = result.returncode == 0
        # Integration must not silently verify a moving revision set.
        stable = revision == self.integration_revision(self.storage.load_task(task.task_id))
        passed = passed and stable
        self.storage.write_artifact(
            task.task_id,
            "artifacts/integration.json",
            json.dumps(
                {
                    "revision": revision,
                    "command": command,
                    "passed": passed,
                    "returncode": result.returncode,
                    "stable": stable,
                    "output": (result.stdout + result.stderr)[-100_000:],
                },
                indent=2,
            ),
        )
        self.storage.append_event(
            task.task_id,
            TaskEvent(
                type="verification.integration", data={"revision": revision, "passed": passed}
            ),
        )
        return passed

    async def maybe_complete(self, task: TaskRecord) -> TaskRecord:
        current = self.storage.load_task(task.task_id)
        if not self.all_deployments_verified(current):
            return current
        merged_values = [
            _value(current, repository, "merged", None)
            for repository in self.changed_repositories(current)
            if self.requires_remote_deployment(repository)
        ]
        if any(value is not None for value in merged_values) and not all(merged_values):
            return current
        if not await self.run_integration(current):
            return self.storage.transition(
                current.task_id,
                TaskState.BLOCKED,
                error="application integration verification failed",
            )
        completed = self.storage.transition(current.task_id, TaskState.COMPLETED)
        self._write_group_artifact(completed)
        lines = [f"# Application result: {completed.external_id}", ""]
        for repository in self.changed_repositories(completed):
            lines.extend(
                [
                    f"## {repository}",
                    f"- Branch: `{_value(completed, repository, 'branch') or ''}`",
                    "- Pull request: "
                    f"{_value(completed, repository, 'pr_url') or 'local publication'}",
                    f"- Commit: `{_value(completed, repository, 'pr_head_sha') or ''}`",
                    "- Local verification: "
                    f"{_value(completed, repository, 'verification_status') or 'not recorded'}",
                    "- Deployment verification: "
                    f"{_value(completed, repository, 'playwright_status') or 'not applicable'}",
                    "",
                ]
            )
        lines.append("- Application integration: passed")
        self.storage.write_artifact(completed.task_id, "result.md", "\n".join(lines) + "\n")
        return completed


__all__ = ["ApplicationWorkflow", "_state", "_value", "_update_state", "_view"]
