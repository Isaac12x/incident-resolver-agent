"""Catalog-backed incident workflow and event routing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import subprocess
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from .agent import IncidentAgent
from .application_workflow import ApplicationWorkflow
from .application_workflow import _update_state as update_application_state
from .application_workflow import _value as application_value
from .code_review import review
from .config import Config, RepositoryConfig
from .github import GitHubService
from .intelligence import (
    LogisticRootCauseModel,
    SimilarIncidentSearch,
    summarize_incident,
)
from .lifecycle_graph import ACTIVE_STATES, TERMINAL_STATES
from .models import (
    DeploymentReference,
    Incident,
    PullRequestReference,
    ReviewComment,
    TaskEvent,
    TaskRecord,
    TaskState,
    VerificationResult,
)
from .operations import OperationBudgetExceeded, OperationLedger
from .storage import RepositoryBusyError, Storage
from .tooling import ToolResult
from .tools import WorkspaceTools
from .verification_graph import VerificationGraph
from .verify import DeploymentVerifier

GraphIndexer = Callable[[Path], ToolResult]


class _TaskLifecycle:
    """Validated workflow mutations callable from one durable task agent session."""

    def __init__(self, workflow: WorkflowEngine, task_id: str, worktree: Path) -> None:
        self.workflow = workflow
        self.task_id = task_id
        self.worktree = worktree

    def _task(self) -> TaskRecord:
        return self.workflow.storage.load_task(self.task_id)

    async def mark_investigation_complete(
        self,
        root_cause: str,
        evidence: list[str],
        proposed_fix: str,
        reproducible: bool = False,
    ) -> dict[str, Any]:
        if not root_cause.strip() or not proposed_fix.strip():
            raise ValueError("root cause and proposed fix are required")
        task = self._task()
        if task.state not in {
            TaskState.COLLECTING_CONTEXT,
            TaskState.INVESTIGATING,
            TaskState.REPRODUCING,
        }:
            raise RuntimeError(f"investigation cannot complete from {task.state.value}")
        markdown = (
            "# Investigation\n\n## Root Cause\n\n"
            + root_cause.strip()
            + "\n\n## Evidence\n\n"
            + "\n".join(f"- {item}" for item in evidence)
            + "\n\n## Proposed Fix\n\n"
            + proposed_fix.strip()
            + "\n"
        )
        self.workflow.storage.write_artifact(self.task_id, "investigation.md", markdown)
        self.workflow.storage.append_task_memory(
            self.task_id,
            f"## Investigation\n\nRoot cause: {root_cause.strip()}\n\n"
            f"Proposed fix: {proposed_fix.strip()}\n",
        )
        self.workflow.storage.record_incident_history(
            self.task_id, self.workflow.storage.load_incident(self.task_id), root_cause=root_cause
        )
        if task.state == TaskState.COLLECTING_CONTEXT:
            task = self.workflow.storage.transition(self.task_id, TaskState.INVESTIGATING)
        reproduced = (
            await self.workflow.reproducer(task, self.worktree)
            if self.workflow.reproducer
            else reproducible
        )
        self.workflow.storage.append_event(
            self.task_id,
            TaskEvent(type="incident.reproduction", data={"reproduced": reproduced}),
        )
        task = self.workflow.storage.transition(self.task_id, TaskState.REPRODUCING)
        return {"state": task.state.value, "reproduced": reproduced}

    def _verification(self, repository: str | None = None) -> VerificationGraph:
        relative = "artifacts/local/verification-graph.json"
        if repository:
            relative = f"artifacts/local/{repository.replace('/', '--')}/verification-graph.json"
        return VerificationGraph(
            self.worktree
            if repository is None
            else self.workflow.application.worktree(self._task(), repository),
            self.workflow.storage.task_directory(self.task_id) / relative,
        )

    async def verification_plan(
        self, seed_paths: list[str], repository: str | None = None
    ) -> dict[str, Any]:
        """Return the next concentric ring; seed regression must pass first."""
        if self.workflow.repository_indexer:
            result = await asyncio.to_thread(self.workflow.repository_indexer, self.worktree)
            if not result.succeeded:
                raise RuntimeError("cannot plan verification with a failed graph refresh")
        task = self._task()
        repository_name = repository or task.repository
        configured = self.workflow.config.repository(repository_name)
        ledger = self._verification(repository)
        if self.workflow._is_application(task):
            if seed_paths:
                return ledger.plan(seed_paths, configured.responsibility_paths)
            return self.workflow.application.verification_plan(
                ledger, configured.responsibility_paths
            )
        return ledger.plan(seed_paths, configured.responsibility_paths)

    async def run_tests(
        self,
        command: str,
        paths: list[str] | None = None,
        force: bool = False,
        repository: str | None = None,
    ) -> dict[str, Any]:
        task = self._task()
        if task.state not in {
            TaskState.REPRODUCING,
            TaskState.IMPLEMENTING,
            TaskState.TESTING_LOCAL,
            TaskState.WAITING_FOR_REVIEW,
        }:
            raise RuntimeError(f"tests cannot run from {task.state.value}")
        repository_name = repository or task.repository
        repository_config = self.workflow.config.repository(repository_name)
        selected_worktree = (
            self.workflow.application.worktree(task, repository_name)
            if self.workflow._is_application(task)
            else self.worktree
        )
        review_task = (
            self.workflow.application.view(task, repository_name)
            if self.workflow._is_application(task)
            else task
        )
        if (
            "playwright" in command.casefold() or command == repository_config.playwright.command
        ) and not await self.workflow._review_fix(review_task, selected_worktree):
            return self.workflow._review_feedback(self.task_id)
        if task.state != TaskState.IMPLEMENTING:
            task = self.workflow.storage.transition(self.task_id, TaskState.IMPLEMENTING)
        tools = WorkspaceTools(
            selected_worktree,
            timeout=self.workflow.config.model.tool_timeout_seconds,
            permissions=self.workflow.config.permissions,
            execution=self.workflow.config.execution,
            logger=lambda data: self.workflow.storage.append_event(
                self.task_id, TaskEvent(type=str(data.pop("type")), data=data)
            ),
        )
        ledger = self._verification(repository)
        if paths:
            paths = sorted({ledger.relative(path) for path in paths})
        if ledger.data["seeds"]:
            plan = (
                self.workflow.application.verification_plan(
                    ledger, repository_config.responsibility_paths
                )
                if self.workflow._is_application(task)
                else ledger.plan([], repository_config.responsibility_paths)
            )
            if paths and plan["next_ring"] is not None:
                permitted = set().union(
                    *(set(ring) for ring in plan["rings"][: plan["next_ring"] + 1])
                )
                nodes, _ = ledger.graph()
                permitted.update(node["file_path"] for node in nodes if node["kind"] == "Test")
                if not set(paths) <= permitted:
                    raise RuntimeError("verify the current ring before expanding outward")
        inputs = ledger.snapshot(paths, command)
        cached = (
            None if force or self.workflow.local_tester else ledger.cached(command, paths, inputs)
        )
        if cached:
            self.workflow.storage.transition(self.task_id, TaskState.TESTING_LOCAL, error=None)
            if self.workflow._is_application(task):
                current = self.workflow.storage.load_task(self.task_id)
                update_application_state(
                    current,
                    repository_name,
                    verification_status="passed",
                    verification_command=command,
                    verification_output=str(cached.get("stdout", ""))[-100_000:],
                )
                self.workflow.storage.save_task(current)
            self.workflow.storage.append_event(
                self.task_id,
                TaskEvent(
                    type="verification.reused",
                    data={"command": command, "paths": paths},
                ),
            )
            return {**cached, "state": TaskState.TESTING_LOCAL.value, "cached": True}
        result = await tools.shell(command)
        passed = result.returncode == 0
        if passed and self.workflow.local_tester:
            passed = await self.workflow.local_tester(task, selected_worktree)
        stable = inputs == ledger.snapshot(paths, command)
        ledger.record(
            command,
            paths,
            inputs,
            {
                "passed": passed and stable,
                "returncode": result.returncode,
                "stdout": result.stdout[-2000:],
                "stderr": result.stderr[-2000:],
            },
        )
        self.workflow.storage.append_event(
            self.task_id,
            TaskEvent(
                type="verification.local",
                data={
                    "repository": repository_name,
                    "command": command,
                    "returncode": result.returncode,
                    "passed": passed,
                    "stdout": result.stdout[-2000:],
                    "stderr": result.stderr[-2000:],
                },
            ),
        )
        if passed:
            task = self.workflow.storage.transition(
                self.task_id, TaskState.TESTING_LOCAL, error=None
            )
            if self.workflow._is_application(task):
                current = self.workflow.storage.load_task(self.task_id)
                update_application_state(
                    current,
                    repository_name,
                    verification_status="passed",
                    verification_command=command,
                    verification_output=(result.stdout + result.stderr)[-100_000:],
                )
                self.workflow.storage.save_task(current)
        else:
            attempts = task.attempts + 1
            state = (
                TaskState.BLOCKED
                if attempts >= self.workflow.config.model.max_task_iterations
                else TaskState.REPRODUCING
            )
            task = self.workflow.storage.transition(
                self.task_id,
                state,
                attempts=attempts,
                error=f"local verification failed: {command}",
            )
        return {
            "state": task.state.value,
            "passed": passed,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "cached": False,
            "inputs_stable": stable,
        }

    async def open_pr(self, summary: str) -> dict[str, Any]:
        task = self._task()
        if task.state not in {TaskState.TESTING_LOCAL, TaskState.PUBLISHING_PR}:
            raise RuntimeError("pull requests require successful local verification")
        if self.workflow._is_application(task):
            for repository in self.workflow.application.repositories(task):
                ledger = self._verification(repository)
                pending = (
                    self.workflow.application.verification_pending(ledger)
                    if self.workflow._is_application(task)
                    else ledger.pending_checks()
                )
                if pending:
                    raise RuntimeError(
                        "pull requests require current passing results for every local check"
                    )
                if ledger.data["seeds"]:
                    plan = await self.verification_plan([], repository)
                    if plan["next_ring"] is not None:
                        raise RuntimeError(
                            f"complete verification of {repository} before publishing"
                        )
            self.workflow.storage.write_artifact(
                self.task_id, "artifacts/local/fix.txt", summary.strip()
            )
            task = self._task()
            if task.state != TaskState.PUBLISHING_PR:
                task = self.workflow.storage.transition(self.task_id, TaskState.PUBLISHING_PR)
            task = await self.workflow.application.publish(task)
            if task.state not in {TaskState.PUBLISHING_PR, TaskState.WAITING_FOR_DEPLOYMENT}:
                return self.workflow._review_feedback(self.task_id)
            local_only = all(
                self.workflow.config.repository(repository).publish_mode == "local"
                or (
                    self.workflow.config.repository(repository).publish_mode == "auto"
                    and self.workflow.github.api is None
                )
                for repository in self.workflow.application.changed_repositories(task)
            )
            if local_only:
                task = await self.workflow.application.maybe_complete(task)
            else:
                task = self.workflow.storage.transition(
                    self.task_id, TaskState.WAITING_FOR_DEPLOYMENT, error=None
                )
            return {
                "state": task.state,
                "application": task.application,
                "repositories": {
                    repository: {
                        "branch": member.branch,
                        "pr_number": member.pr_number,
                        "url": member.pr_url,
                        "head_sha": member.pr_head_sha,
                    }
                    for repository, member in task.repositories.items()
                    if member.changed
                },
            }
        ledger = self._verification()
        if ledger.pending_checks():
            raise RuntimeError(
                "pull requests require current passing results for every local check"
            )
        if (self.worktree / ".code-review-graph/graph.db").exists() or ledger.data["seeds"]:
            plan = await self.verification_plan([])
            if plan["next_ring"] is not None:
                raise RuntimeError(
                    "complete verification of the responsibility area before publishing"
                )
        self.workflow.storage.write_artifact(
            self.task_id, "artifacts/local/fix.txt", summary.strip()
        )
        if self.workflow.config.code_review.enabled and not await self.workflow._review_fix(
            task, self.worktree
        ):
            return self.workflow._review_feedback(self.task_id)
        task = self._task()
        self.workflow.storage.append_task_memory(
            self.task_id, f"## Verified fix\n\n{summary.strip()}\n"
        )
        repository = self.workflow.config.repository(task.repository)
        if task.pr_number and self.workflow.github.api is not None:
            reference = await self.workflow._publish(task, self.worktree, update=True)
            if reference is None:
                raise RuntimeError("GitHub did not confirm the updated pull request")
            task = self.workflow.storage.transition(
                self.task_id,
                TaskState.WAITING_FOR_DEPLOYMENT,
                pr_head_sha=reference.head_sha,
                pr_url=reference.url,
            )
        elif task.pr_number:
            reference = await self.workflow._publish(task, self.worktree, update=True)
            task = self.workflow.storage.transition(
                self.task_id,
                TaskState.WAITING_FOR_DEPLOYMENT,
                pr_head_sha=reference.head_sha,
                pr_url=reference.url,
            )
        elif repository.publish_mode == "local" or (
            repository.publish_mode == "auto" and self.workflow.github.api is None
        ):
            if task.state != TaskState.PUBLISHING_PR:
                task = self.workflow.storage.transition(self.task_id, TaskState.PUBLISHING_PR)
            task = self.workflow._local_publish(task, self.worktree)
        else:
            if task.state != TaskState.PUBLISHING_PR:
                task = self.workflow.storage.transition(self.task_id, TaskState.PUBLISHING_PR)
            pull_request = await self.workflow._publish(task, self.worktree)
            task = self.workflow.storage.transition(
                self.task_id,
                TaskState.WAITING_FOR_DEPLOYMENT,
                branch=pull_request.branch,
                pr_number=pull_request.number,
                pr_url=pull_request.url,
                pr_head_sha=pull_request.head_sha,
            )
        return {
            "state": task.state.value,
            "url": task.pr_url,
            "head_sha": task.pr_head_sha,
        }

    async def remember(self, note: str, scope: str = "task") -> dict[str, Any]:
        if not note.strip():
            raise ValueError("memory note cannot be blank")
        task = self._task()
        if scope == "task":
            self.workflow.storage.append_task_memory(self.task_id, note)
        elif scope == "repository":
            self.workflow.storage.append_memory(note, task.repository)
        else:
            raise ValueError("memory scope must be task or repository")
        self.workflow.storage.append_event(
            self.task_id, TaskEvent(type="agent.memory_written", data={"scope": scope})
        )
        return {"stored": True, "scope": scope}


class WorkflowEngine:
    def __init__(
        self,
        config: Config,
        storage: Storage,
        agent: IncidentAgent,
        github: GitHubService,
        verifier: DeploymentVerifier,
        *,
        context_collector: Callable[[TaskRecord, Path], Awaitable[str]] | None = None,
        reproducer: Callable[[TaskRecord, Path], Awaitable[bool]] | None = None,
        local_tester: Callable[[TaskRecord, Path], Awaitable[bool]] | None = None,
        repository_indexer: GraphIndexer | None = None,
    ) -> None:
        self.config = config
        self.storage = storage
        self.agent = agent
        self.github = github
        self.verifier = verifier
        self.context_collector = context_collector
        self.reproducer = reproducer
        self.local_tester = local_tester
        self.repository_indexer = repository_indexer
        self.reload_model: Callable[[], None] | None = None
        self._wakeups: asyncio.Queue[str] = asyncio.Queue()
        self._stopping = asyncio.Event()
        self._review_comments: dict[str, list[ReviewComment]] = {}
        self._queued_task_ids: set[str] = set()
        self._running_task_ids: set[str] = set()
        self._deferred_wakeups: set[str] = set()
        self.root_cause_model = LogisticRootCauseModel([], [], {}, 0)
        self.similar_incidents = SimilarIncidentSearch(allow_download=False)
        self._intelligence_lock = threading.RLock()
        self._history_signature = self._current_history_signature()
        self._intelligence_initialized = False
        self.application = ApplicationWorkflow(self)

    def _is_application(self, task: TaskRecord) -> bool:
        return self.application.is_application(task)

    def _operations(self, task_id: str) -> OperationLedger:
        return OperationLedger(
            self.storage.root / "runtime.sqlite3",
            namespace=task_id,
            max_attempts=self.config.model.max_task_iterations,
            overall_cap=self.config.model.max_task_iterations * 2,
        )

    @contextmanager
    def _operation(self, task: TaskRecord, name: str, revision: str):
        """Persist intent before an effect; charge every replay, even after a crash."""
        ledger = self._operations(task.task_id)
        try:
            ledger.begin(name, revision, reuse_success=False)
        except OperationBudgetExceeded as error:
            self.storage.transition(task.task_id, TaskState.BLOCKED, error=str(error))
            raise
        started = time.monotonic()
        outcome: dict[str, Any] = {}
        try:
            yield outcome
        except Exception as error:
            ledger.finish(
                name,
                revision,
                succeeded=False,
                outcome={"error": str(error), "duration_seconds": time.monotonic() - started},
            )
            raise
        else:
            outcome["duration_seconds"] = time.monotonic() - started
            ledger.finish(name, revision, succeeded=outcome.get("passed", True), outcome=outcome)

    def _publication_revision(self, task: TaskRecord, worktree: Path) -> str:
        # Real Git workspaces include dirty/untracked inputs, not the previous PR SHA.
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=worktree, capture_output=True, text=True
        )
        if head.returncode:
            # Custom publication adapters can operate without a Git checkout. They
            # still share the task cap and never reuse a cached publication result.
            return task.pr_head_sha or task.branch or task.task_id
        inputs = VerificationGraph(worktree, self.storage.root / "unused-verification.json").files()
        return hashlib.sha256(
            json.dumps([head.stdout.strip(), inputs], sort_keys=True).encode()
        ).hexdigest()

    async def _publish(
        self, task: TaskRecord, worktree: Path, *, update: bool = False
    ) -> PullRequestReference:
        revision = self._publication_revision(task, worktree)
        with self._operation(task, "publish", revision) as outcome:
            if update and self.github.api is None:
                tools = WorkspaceTools(worktree)
                result = await tools.shell("git rev-parse HEAD")
                if result.returncode:
                    raise RuntimeError(result.stderr or "could not determine updated PR head")
                if not task.branch:
                    raise RuntimeError("cannot update a pull request without its branch")
                push = await tools.shell(
                    f"git -c remote.origin.mirror=false push origin HEAD:{task.branch}"
                )
                if push.returncode:
                    raise RuntimeError(
                        push.stderr or "could not push the updated pull-request head"
                    )
                updated = task.model_copy(update={"pr_head_sha": result.stdout.strip()})
                reference = await self.github.update_pull_request(updated)
                reference = reference or PullRequestReference(
                    repository=task.repository,
                    number=task.pr_number,
                    branch=task.branch,
                    head_sha=result.stdout.strip(),
                    url=task.pr_url or "",
                )
            else:
                reference = (
                    await self.github.update_pull_request(task)
                    if update
                    else await self.github.create_pull_request(task)
                )
                if reference is None:
                    raise RuntimeError("GitHub did not confirm the updated pull request")
            outcome.update(reference.model_dump(mode="json"))
            return reference

    def _current_history_signature(self) -> tuple[tuple[str, str | None], ...]:
        return tuple(
            (str(item.get("task_id")), item.get("root_cause"))
            for item in self.storage.incident_history(limit=1000)
        )

    def _refresh_intelligence(self) -> None:
        signature = self._current_history_signature()
        if self._intelligence_initialized and signature == self._history_signature:
            return
        records = self.storage.incident_history(labeled_only=True)
        self.root_cause_model = LogisticRootCauseModel.train(records)
        self.similar_incidents = SimilarIncidentSearch(allow_download=False)
        self._history_signature = signature
        self._intelligence_initialized = True

    def _has_review_comments(self, task_id: str) -> bool:
        task = self.storage.load_task(task_id)
        if task.pending_review_comments or self._review_comments.get(task_id):
            return True
        if self._is_application(task):
            return any(
                application_value(task, repository, "pending_review_comments", [])
                for repository in self.application.repositories(task)
            )
        return False

    def _take_review_comments(self, task_id: str) -> list[ReviewComment]:
        task = self.storage.load_task(task_id)
        comments = list(task.pending_review_comments)
        comments.extend(self._review_comments.pop(task_id, []))
        if self._is_application(task):
            for repository in self.application.repositories(task):
                comments.extend(
                    application_value(task, repository, "pending_review_comments", []) or []
                )
                update_application_state(task, repository, pending_review_comments=[])
            self.storage.save_task(task)
        unique = {comment.id: comment for comment in comments}
        if task.pending_review_comments:
            task.pending_review_comments = []
            self.storage.save_task(task)
        return list(unique.values())

    def _queue_review_comments(
        self,
        task_id: str,
        comments: list[ReviewComment],
        repository: str | None = None,
    ) -> None:
        task = self.storage.load_task(task_id)
        if self._is_application(task):
            if comments:
                repository = repository or getattr(comments[0], "repository", None)
            repository = repository or task.repository
            existing = application_value(task, repository, "pending_review_comments", []) or []
            known = {getattr(comment, "id", None) for comment in existing}
            update_application_state(
                task,
                repository,
                pending_review_comments=existing
                + [comment for comment in comments if getattr(comment, "id", None) not in known],
            )
            self.storage.save_task(task)
            return
        known = {comment.id for comment in task.pending_review_comments}
        task.pending_review_comments.extend(
            comment for comment in comments if comment.id not in known
        )
        self.storage.save_task(task)
        self._review_comments[task_id] = list(task.pending_review_comments)

    def _ack_application_review_comments(
        self, task_id: str, acknowledged: dict[str, set[int]]
    ) -> None:
        task = self.storage.load_task(task_id)
        if not self._is_application(task):
            return
        for repository, ids in acknowledged.items():
            comments = application_value(task, repository, "pending_review_comments", []) or []
            update_application_state(
                task,
                repository,
                pending_review_comments=[comment for comment in comments if comment.id not in ids],
            )
        self.storage.save_task(task)

    async def submit(self, incident: Incident) -> TaskRecord:
        selected_application = None
        selected_application = self.config.resolve_application(
            application=incident.application,
            service=incident.service,
            repository=incident.repository or None,
        )
        if selected_application:
            primary = incident.repository or selected_application.repositories[0]
            incident = incident.model_copy(
                update={"repository": primary, "application": selected_application.name}
            )
        if selected_application:
            for name in selected_application.repositories:
                repository = self.config.repository(name)
                if incident.environment not in repository.incident_environments:
                    raise ValueError(
                        f"environment {incident.environment!r} is not enabled for {name}"
                    )
            task = self.storage.create_task(incident, selected_application)
            self.storage.record_incident_history(task.task_id, incident)
            if task.state == TaskState.RECEIVED:
                await self.wake(task.task_id)
            return task
        try:
            repository = self.config.repository(incident.repository)
        except KeyError as error:
            if self.storage.local_repository(incident.repository) is None:
                raise ValueError(str(error)) from error
            repository = RepositoryConfig(name=incident.repository, publish_mode="local")
            self.config.repositories.append(repository)
        if incident.environment not in repository.incident_environments:
            raise ValueError(
                f"environment {incident.environment!r} is not enabled for {incident.repository}"
            )
        existing = self.storage.find_by_incident(
            incident.source, incident.external_id, incident.repository, incident.environment
        )
        task = self.storage.create_task(incident)
        self.storage.record_incident_history(task.task_id, incident)
        if existing is None:
            await self.wake(task.task_id)
        return task

    def intelligence_summary(self, task_id: str) -> dict[str, Any]:
        incident = self.storage.load_incident(task_id)
        text = " ".join(filter(None, (incident.summary, incident.description)))
        directory = self.storage.task_directory(task_id)
        for source in ("artifacts/local/fix.txt", "investigation.md"):
            artifact = directory / source
            if artifact.is_file():
                summary = artifact.read_text(encoding="utf-8").strip()
                if summary:
                    return {
                        "summary": summary,
                        "method": "agent-artifact",
                        "source": source,
                        "format": "markdown",
                    }
        return {
            **summarize_incident(text),
            "method": "extractive-fallback",
            "reason": "agent investigation summary is not available yet",
        }

    def _intelligence_context(self, task_id: str) -> dict[str, Any]:
        """Build bounded, durable context used by the agent for every new intake."""
        incident = self.storage.load_incident(task_id)
        text = " ".join(filter(None, (incident.summary, incident.description)))
        with self._intelligence_lock:
            self._refresh_intelligence()
            prediction = self.root_cause_model.predict(text)
            related = self.similar_incidents_search(text, limit=5)
            related["results"] = [
                item for item in related.get("results", []) if item.get("task_id") != task_id
            ]
        return {
            "schema_version": 1,
            "summary": self.intelligence_summary(task_id),
            "prediction": prediction,
            "related_incidents": related,
        }

    def predict_root_cause(self, text: str) -> dict[str, Any]:
        with self._intelligence_lock:
            self._refresh_intelligence()
            return self.root_cause_model.predict(text)

    def similar_incidents_search(self, text: str, limit: int = 5) -> dict[str, Any]:
        with self._intelligence_lock:
            self._refresh_intelligence()
            if self.similar_incidents._index is None:
                self.similar_incidents.build(self.storage.incident_history())
            return self.similar_incidents.search(text, limit=limit)

    def rebuild_intelligence(self) -> dict[str, Any]:
        with self._intelligence_lock:
            records = self.storage.incident_history(labeled_only=True)
            self.root_cause_model = LogisticRootCauseModel.train(records)
            vector = self.similar_incidents.build(self.storage.incident_history())
            self._history_signature = self._current_history_signature()
            self._intelligence_initialized = True
        return {
            "root_cause": {
                "available": bool(self.root_cause_model.weights),
                "trained_samples": self.root_cause_model.trained_samples,
            },
            "similar_incidents": vector,
        }

    async def wake(self, task_id: str) -> None:
        if task_id in self._running_task_ids:
            self._deferred_wakeups.add(task_id)
            return
        if task_id in self._queued_task_ids:
            return
        self._queued_task_ids.add(task_id)
        await self._wakeups.put(task_id)

    def cancel(self, task_id: str) -> TaskRecord:
        task = self.storage.load_task(task_id)
        if task.state in TERMINAL_STATES:
            return task
        return self.storage.transition(task_id, TaskState.CANCELLED)

    async def _worktree(self, task: TaskRecord) -> Path:
        if self._is_application(task):
            # The durable lead session uses the application parent workspace;
            # repository tools select a member checkout explicitly.  Prefer the
            # storage-provided parent when available and retain a deterministic
            # fallback for older task snapshots.
            parent = self.storage.root / "worktrees" / task.task_id
            create = getattr(self.storage, "create_application_worktrees", None)
            created_application = False
            if create and not parent.exists():
                await asyncio.to_thread(create, task, self.config)
                created_application = True
            parent.mkdir(parents=True, exist_ok=True)
            if not create or not created_application:
                self.storage.catalog.verify_workspace(task.task_id, parent)
            return parent
        worktree = self.storage.root / "worktrees" / task.task_id
        if not worktree.exists():
            repository = self.config.repository(task.repository)
            worktree = await asyncio.to_thread(
                self.storage.create_worktree,
                task,
                repository.clone_url,
                repository.base_branch,
                repository.local_path,
            )
        self.storage.catalog.verify_workspace(task.task_id, worktree)
        graph_marker = (
            self.storage.task_directory(task.task_id) / "artifacts" / "repository" / "graphs-ready"
        )
        if self.repository_indexer and not graph_marker.exists():
            result = await asyncio.to_thread(self.repository_indexer, worktree)
            self.storage.append_event(
                task.task_id,
                TaskEvent(
                    type="repository.graph_indexed",
                    data={
                        "tool": "code-review-graph",
                        "command": list(result.command),
                        "returncode": result.returncode,
                        "stderr": result.stderr[-2000:],
                    },
                ),
            )
            if not result.succeeded:
                detail = f"{result.command[0]} exited {result.returncode}: {result.stderr.strip()}"
                raise RuntimeError(f"repository graph generation failed: {detail}")
            self.storage.write_artifact(
                task.task_id,
                "artifacts/repository/graphs-ready",
                "code-review-graph completed\n",
            )
        return worktree

    def _local_publish(self, task: TaskRecord, worktree: Path) -> TaskRecord:
        with self._operation(
            task, "publish", self._publication_revision(task, worktree)
        ) as outcome:
            result = self._local_publish_effect(task, worktree)
            outcome.update(head_sha=result.pr_head_sha, url=result.pr_url)
            return result

    def _local_publish_effect(self, task: TaskRecord, worktree: Path) -> TaskRecord:
        sha = self.storage.commit_worktree(task, f"Fix incident {task.external_id}")
        reference = PullRequestReference(
            repository=task.repository,
            number=0,
            url=f"local://{task.task_id}",
            head_sha=sha,
            branch=task.branch or "",
        )
        self.storage._json_write(
            self.storage.task_directory(task.task_id) / "pr.json",
            reference.model_dump(mode="json"),
        )
        self.storage.write_artifact(
            task.task_id,
            "result.md",
            "# Local Result\n\n"
            f"Resolved `{task.external_id}` locally.\n\n"
            f"Branch: `{reference.branch}`\n\n"
            f"Commit: `{sha}`\n\n"
            "The change was committed locally; GitHub publishing was not configured.\n",
        )
        self.storage.write_artifact(
            task.task_id, "artifacts/local/final.diff", self.storage.worktree_diff(task)
        )
        return self.storage.transition(
            task.task_id,
            TaskState.COMPLETED,
            pr_url=reference.url,
            pr_head_sha=sha,
        )

    def _review_feedback(self, task_id: str) -> dict[str, Any]:
        task = self.storage.load_task(task_id)
        report = self.storage.task_directory(task_id) / "artifacts/code-review/scan-result.json"
        return {
            "state": task.state.value,
            "passed": False,
            "error": task.error,
            "review_report": report.read_text() if report.exists() else None,
        }

    async def _review_fix(self, task: TaskRecord, worktree: Path) -> bool:
        if self.storage.load_task(task.task_id).state in TERMINAL_STATES:
            return False
        if not self.config.code_review.enabled:
            return True
        repository_name = task.repository

        def git(*args: str) -> str:
            return subprocess.run(
                ["git", *args],
                cwd=worktree,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            ).stdout.strip()

        status_args = (
            "status",
            "--porcelain",
            "--",
            ".",
            ":(exclude)harness-out",
            ":(exclude).code-review-graph",
            ":(exclude).code-review-graph.db",
        )
        try:
            dirty = await asyncio.to_thread(git, *status_args)
            if dirty:
                if task.state == TaskState.TESTING_DEPLOYMENT:
                    raise ValueError("worktree changed after deployment; publish the fix again")
                if self.config.permissions.mode != "workspace":
                    raise ValueError("OCR cannot commit changes with read-only permissions")
                if getattr(task, "repositories", None):
                    await asyncio.to_thread(
                        self.storage.commit_worktree,
                        task,
                        f"Fix incident {task.external_id}",
                        repository_name,
                    )
                else:
                    await asyncio.to_thread(
                        self.storage.commit_worktree, task, f"Fix incident {task.external_id}"
                    )
            head = await asyncio.to_thread(git, "rev-parse", "HEAD")
            if task.state == TaskState.TESTING_DEPLOYMENT and head != task.pr_head_sha:
                raise ValueError("OCR checkout does not match the current PR head")
            member_state = (
                task.repository_state(repository_name)
                if getattr(task, "repositories", None)
                else None
            )
            if (member_state.code_review_sha if member_state else task.code_review_sha) == head:
                return True
            branch = await asyncio.to_thread(git, "branch", "--show-current")
            if not branch or (task.branch and branch != task.branch):
                raise ValueError("OCR requires the incident feature branch checkout")
            base = self.config.repository(repository_name).base_branch
            # Managed clones may have only a remote-tracking base branch.
            try:
                await asyncio.to_thread(git, "rev-parse", "--verify", f"{base}^{{commit}}")
            except subprocess.CalledProcessError:
                base = f"origin/{base}"
                await asyncio.to_thread(git, "rev-parse", "--verify", f"{base}^{{commit}}")
            review_slug = (
                repository_name.replace("/", "--") if getattr(task, "repositories", None) else ""
            )
            output = (
                self.storage.task_directory(task.task_id) / "artifacts/code-review" / review_slug
            )
            report = await asyncio.to_thread(
                review, self.config, worktree, base, branch, output / "scan-result.json"
            )
            self.storage.write_artifact(
                task.task_id,
                f"artifacts/code-review/{review_slug}/{head}.json",
                json.dumps(report, indent=2),
            )
            if await asyncio.to_thread(git, "rev-parse", "HEAD") != head:
                raise ValueError("checkout changed during OCR review")
            if await asyncio.to_thread(git, *status_args):
                raise ValueError("worktree changed during OCR review")
            self.storage.append_event(
                task.task_id,
                TaskEvent(
                    type="verification.code_review",
                    data={"sha": head, "findings": len(report["comments"])},
                ),
            )
            task = self.storage.load_task(task.task_id)
            if report["comments"]:
                attempts = task.attempts + 1
                if getattr(task, "repositories", None):
                    current = self.storage.load_task(task.task_id)
                    review_error = (
                        "Address OCR findings in the repository-specific code review report, "
                        "then rerun local checks and publish before Playwright verification"
                    )
                    update_application_state(
                        current,
                        repository_name,
                        attempts=attempts,
                        code_review_sha=None,
                        playwright_status=None,
                        error=review_error,
                    )
                    self.storage.save_task(current)
                    self.storage.transition(
                        task.task_id,
                        TaskState.BLOCKED
                        if attempts >= self.config.model.max_task_iterations
                        else TaskState.REPRODUCING,
                        error=review_error,
                    )
                else:
                    self.storage.transition(
                        task.task_id,
                        TaskState.BLOCKED
                        if attempts >= self.config.model.max_task_iterations
                        else TaskState.REPRODUCING,
                        attempts=attempts,
                        code_review_sha=None,
                        playwright_status=None,
                        error="Address OCR findings in artifacts/code-review/scan-result.json, "
                        "then rerun local checks and publish before Playwright verification",
                    )
                return False
            if getattr(task, "repositories", None):
                current = self.storage.load_task(task.task_id)
                update_application_state(current, repository_name, code_review_sha=head)
                self.storage.save_task(current)
            else:
                task.code_review_sha = head
                self.storage.save_task(task)
            return True
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            # Do not persist subprocess stderr: provider errors may contain credentials.
            detail = str(error) if isinstance(error, ValueError) else type(error).__name__
            self.storage.transition(
                task.task_id,
                TaskState.BLOCKED,
                code_review_sha=None,
                error=f"Open Code Review could not complete: {detail}",
            )
            return False

    async def _process_agent_session(self, task: TaskRecord, worktree: Path) -> TaskRecord:
        lifecycle = _TaskLifecycle(self, task.task_id, worktree)
        prompt: str | None = None
        application_review = False
        acknowledged_reviews: dict[str, set[int]] = {}
        if task.state == TaskState.COLLECTING_CONTEXT:
            context = self.storage.task_directory(task.task_id) / "context.md"
            if context.exists():
                prompt = (
                    "Resolve this incident end to end in the durable task session. The collected "
                    "incident context follows.\n\n" + context.read_text(encoding="utf-8")
                )
        elif task.state == TaskState.WAITING_FOR_REVIEW:
            if self._is_application(task):
                application_review = True
                sections: list[str] = []
                for repository in self.application.repositories(task):
                    comments = (
                        application_value(task, repository, "pending_review_comments", []) or []
                    )
                    if comments:
                        acknowledged_reviews[repository] = {comment.id for comment in comments}
                        sections.append(
                            f"Repository: {repository}\n"
                            + "\n".join(
                                f"{comment.id} {comment.path or ''}:{comment.line or ''} "
                                f"{comment.url or ''} {comment.author}: {comment.body}"
                                for comment in comments
                            )
                        )
                prompt = (
                    "Address these authorized review comments in the same task session:\n\n"
                    + "\n\n".join(sections)
                )
            else:
                comments = self._take_review_comments(task.task_id)
                prompt = (
                    "Address these authorized review comments in the same task session:\n\n"
                    + ("\n".join(f"{comment.author}: {comment.body}" for comment in comments))
                )
        else:
            prompt = (
                f"Resume the same durable incident session from state `{task.state.value}`. "
                f"The last recorded error was: {task.error or 'none'}. Continue toward a verified "
                "pull request using lifecycle tools."
            )
        result = await self.agent.run_session(task, worktree, lifecycle, prompt)
        if application_review:
            # Keep per-repository feedback durable if the backend raises or is
            # cancelled; clear it only after a successful session checkpoint.
            self._ack_application_review_comments(task.task_id, acknowledged_reviews)
        self.storage.append_task_memory(
            task.task_id, f"## Session checkpoint\n\n{result.summary.strip()}\n"
        )
        latest = self.storage.load_task(task.task_id)
        if result.blocked_reason and latest.state not in TERMINAL_STATES:
            return self.storage.transition(
                task.task_id, TaskState.BLOCKED, error=result.blocked_reason
            )
        if result.waiting_for_external_event and latest.state not in {
            TaskState.WAITING_FOR_DEPLOYMENT,
            TaskState.WAITING_FOR_REVIEW,
            *TERMINAL_STATES,
        }:
            raise RuntimeError(
                "agent yielded for an external event without a durable lifecycle transition"
            )
        if latest.state == task.state and latest.state in ACTIVE_STATES:
            attempts = latest.attempts + 1
            if attempts >= self.config.model.max_task_iterations:
                return self.storage.transition(
                    task.task_id,
                    TaskState.BLOCKED,
                    attempts=attempts,
                    error="agent session made no durable lifecycle progress",
                )
            latest.attempts = attempts
            latest.error = "agent session made no durable lifecycle progress"
            self.storage.save_task(latest)
        return latest

    async def process(self, task_id: str) -> TaskRecord:
        owner = uuid4().hex
        ttl = max(60, self.config.model.tool_timeout_seconds * 2 + 300)
        if not self.storage.catalog.acquire(task_id, owner, ttl):
            # A crashed owner's lease may still be live. Avoid a tight requeue loop.
            await asyncio.sleep(self.config.poll_interval_seconds)
            return self.storage.load_task(task_id)

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(ttl / 3)
                if not self.storage.catalog.acquire(task_id, owner, ttl):
                    raise RuntimeError("durable task lease was lost")

        renewal = asyncio.create_task(heartbeat())
        work = asyncio.create_task(self._process_unleased(task_id))
        try:
            done, _ = await asyncio.wait({renewal, work}, return_when=asyncio.FIRST_COMPLETED)
            if renewal in done:
                await renewal  # Losing ownership stops work before any further mutation.
            return await work
        finally:
            for pending in (renewal, work):
                pending.cancel()
            for pending in (renewal, work):
                with suppress(asyncio.CancelledError, Exception):
                    await pending
            self.storage.catalog.release(task_id, owner)

    async def _process_application(self, task: TaskRecord) -> TaskRecord | None:
        """Advance application publication/deployment checkpoints.

        Agent investigation and editing still run through the normal durable
        session.  Once that session reaches a cross-repository gate, this method
        performs only the aggregate work and leaves member evidence durable.
        """
        if not self._is_application(task):
            return None
        if task.state != TaskState.RECEIVED:
            self.application.verify_workspaces(task)
        if task.state == TaskState.PUBLISHING_PR:
            await self.application.publish(task)
            current = self.storage.load_task(task.task_id)
            if self.application.all_deployments_verified(current):
                completed = await self.application.maybe_complete(current)
                if completed.state == TaskState.TESTING_DEPLOYMENT:
                    return self.storage.transition(completed.task_id, TaskState.WAITING_FOR_REVIEW)
                return completed
            return self.storage.transition(
                task.task_id, TaskState.WAITING_FOR_DEPLOYMENT, error=None
            )
        pending_deployment = any(
            application_value(task, repository, "deployment_sha")
            and application_value(task, repository, "playwright_status") != "passed"
            and self.application.requires_remote_deployment(repository)
            for repository in self.application.changed_repositories(task)
        )
        if task.state == TaskState.TESTING_DEPLOYMENT or (
            task.state == TaskState.WAITING_FOR_REVIEW and pending_deployment
        ):
            changed = self.application.changed_repositories(task)
            for repository in changed:
                member = self.application.view(self.storage.load_task(task.task_id), repository)
                sha = application_value(task, repository, "deployment_sha")
                url = application_value(task, repository, "deployment_url")
                if (
                    not sha
                    or not url
                    or application_value(task, repository, "playwright_status") == "passed"
                ):
                    continue
                deployment = DeploymentReference(
                    repository=repository,
                    environment=application_value(task, repository, "deployment_environment", ""),
                    sha=str(sha),
                    url=str(url),
                )
                worktree = self.application.worktree(task, repository)
                revision = json.dumps(
                    [
                        repository,
                        application_value(task, repository, "pr_number"),
                        deployment.environment,
                        deployment.sha,
                        deployment.url,
                    ],
                    separators=(",", ":"),
                )
                prior = self._operations(task.task_id).get(
                    f"deployment_verification:{repository}", revision
                )
                if (
                    self.verifier.accepts(member, deployment)
                    and prior
                    and prior["status"] == "succeeded"
                ):
                    result = VerificationResult.model_validate(prior["outcome"])
                else:
                    with self._operation(
                        member, f"deployment_verification:{repository}", revision
                    ) as outcome:
                        result = await self.verifier.verify(member, deployment, worktree)
                        outcome.update(result.model_dump(mode="json"))
                task = self.storage.load_task(task.task_id)
                update_application_state(
                    task, repository, playwright_status="passed" if result.passed else "failed"
                )
                self.storage.append_event(
                    task.task_id,
                    TaskEvent(
                        type="verification.local_deployment",
                        data={"repository": repository, **result.model_dump(mode="json")},
                    ),
                )
                await self.github.publish_verification(member, result)
                if not result.passed:
                    return self.storage.transition(
                        task.task_id, TaskState.REPRODUCING, playwright_status="failed"
                    )
                self.storage.save_task(task)
            current = self.storage.load_task(task.task_id)
            if self.application.all_deployments_verified(current):
                return await self.application.maybe_complete(current)
            if current.state == TaskState.TESTING_DEPLOYMENT:
                return self.storage.transition(current.task_id, TaskState.WAITING_FOR_REVIEW)
            return current
        if task.state == TaskState.WAITING_FOR_REVIEW and not self._has_review_comments(
            task.task_id
        ):
            return await self.application.maybe_complete(task)
        return None

    async def _process_unleased(self, task_id: str) -> TaskRecord:
        task = self.storage.load_task(task_id)
        try:
            application_result = await self._process_application(task)
            if application_result is not None:
                return application_result
            if task.state == TaskState.RECEIVED:
                worktree = await self._worktree(task)
                context = (
                    await self.context_collector(task, worktree)
                    if self.context_collector
                    else self.storage.load_incident(task_id).model_dump_json(indent=2)
                )
                intelligence = await asyncio.to_thread(self._intelligence_context, task_id)
                self.storage.write_artifact(
                    task_id,
                    "artifacts/intelligence.json",
                    json.dumps(intelligence, indent=2) + "\n",
                )
                context += "\n\n## Incident intelligence\n\n" + json.dumps(intelligence, indent=2)
                self.storage.write_artifact(task_id, "context.md", context)
                return self.storage.transition(task_id, TaskState.COLLECTING_CONTEXT)

            worktree = await self._worktree(task)
            self.storage.catalog.verify_workspace(task.task_id, worktree)
            uses_durable_session = bool(
                getattr(self.agent, "supports_durable_session", False)
                and callable(getattr(self.agent, "run_session", None))
            )
            if uses_durable_session and (
                (task.state in ACTIVE_STATES and task.state != TaskState.TESTING_DEPLOYMENT)
                or (
                    task.state == TaskState.WAITING_FOR_REVIEW
                    and self._has_review_comments(task_id)
                )
            ):
                return await self._process_agent_session(task, worktree)
            if task.state == TaskState.COLLECTING_CONTEXT:
                investigation = await self.agent.investigate(task, worktree)
                self.storage.record_incident_history(
                    task_id,
                    self.storage.load_incident(task_id),
                    root_cause=investigation.root_cause,
                )
                markdown = (
                    "# Investigation\n\n## Root Cause\n\n"
                    + investigation.root_cause
                    + "\n\n## Evidence\n\n"
                    + "\n".join(f"- {item}" for item in investigation.evidence)
                    + "\n\n## Proposed Fix\n\n"
                    + investigation.proposed_fix
                    + "\n"
                )
                self.storage.write_artifact(task_id, "investigation.md", markdown)
                return self.storage.transition(task_id, TaskState.INVESTIGATING)

            if task.state == TaskState.INVESTIGATING:
                reproduced = await self.reproducer(task, worktree) if self.reproducer else True
                self.storage.append_event(
                    task_id,
                    TaskEvent(type="incident.reproduction", data={"reproduced": reproduced}),
                )
                return self.storage.transition(task_id, TaskState.REPRODUCING)

            if task.state == TaskState.REPRODUCING:
                result = await self.agent.implement_fix(task, worktree)
                if result.blocked_reason:
                    return self.storage.transition(
                        task_id, TaskState.BLOCKED, error=result.blocked_reason
                    )
                if not result.changed:
                    return self.storage.transition(
                        task_id, TaskState.BLOCKED, error="agent produced no code change"
                    )
                self.storage.write_artifact(task_id, "artifacts/local/fix.txt", result.summary)
                if not result.tests_passed:
                    attempts = task.attempts + 1
                    error = "agent reported that local tests did not pass"
                    if attempts >= self.config.model.max_task_iterations:
                        return self.storage.transition(
                            task_id,
                            TaskState.BLOCKED,
                            attempts=attempts,
                            error=error,
                        )
                    return self.storage.transition(
                        task_id,
                        TaskState.REPRODUCING,
                        attempts=attempts,
                        error=error,
                    )
                return self.storage.transition(task_id, TaskState.IMPLEMENTING, error=None)

            if task.state == TaskState.IMPLEMENTING:
                passed = await self.local_tester(task, worktree) if self.local_tester else True
                if not passed:
                    attempts = task.attempts + 1
                    if attempts >= self.config.model.max_task_iterations:
                        return self.storage.transition(
                            task_id,
                            TaskState.BLOCKED,
                            attempts=attempts,
                            error="local verification retry budget exhausted",
                        )
                    return self.storage.transition(
                        task_id, TaskState.REPRODUCING, attempts=attempts
                    )
                return self.storage.transition(task_id, TaskState.TESTING_LOCAL)

            if task.state == TaskState.TESTING_LOCAL:
                if not await self._review_fix(task, worktree):
                    return self.storage.load_task(task_id)
                task = self.storage.load_task(task_id)
                if task.pr_number and self.config.code_review.enabled:
                    summary = self.storage.task_directory(task_id) / "artifacts/local/fix.txt"
                    await _TaskLifecycle(self, task_id, worktree).open_pr(
                        summary.read_text() if summary.exists() else task.summary
                    )
                    return self.storage.load_task(task_id)
                if task.pr_number:
                    if not task.pr_head_sha:
                        return self.storage.transition(
                            task_id,
                            TaskState.BLOCKED,
                            error="review fix did not report the new PR head SHA",
                        )
                    return self.storage.transition(task_id, TaskState.WAITING_FOR_DEPLOYMENT)
                return self.storage.transition(task_id, TaskState.PUBLISHING_PR)

            if task.state == TaskState.PUBLISHING_PR:
                if not await self._review_fix(task, worktree):
                    return self.storage.load_task(task_id)
                task = self.storage.load_task(task_id)
                repository = self.config.repository(task.repository)
                if repository.publish_mode == "local" or (
                    repository.publish_mode == "auto" and self.github.api is None
                ):
                    return self._local_publish(task, worktree)
                pull_request = await self._publish(task, worktree)
                return self.storage.transition(
                    task_id,
                    TaskState.WAITING_FOR_DEPLOYMENT,
                    branch=pull_request.branch,
                    pr_number=pull_request.number,
                    pr_url=pull_request.url,
                    pr_head_sha=pull_request.head_sha,
                )

            if task.state == TaskState.TESTING_DEPLOYMENT:
                if not await self._review_fix(task, worktree):
                    return self.storage.load_task(task_id)
                task = self.storage.load_task(task_id)
                deployment = DeploymentReference(
                    repository=task.repository,
                    environment=task.deployment_environment or "",
                    sha=task.deployment_sha or "",
                    url=task.deployment_url or "",
                )
                revision = json.dumps(
                    [
                        task.repository,
                        task.pr_number,
                        deployment.environment,
                        deployment.sha,
                        deployment.url,
                    ],
                    separators=(",", ":"),
                )
                prior = self._operations(task_id).get("deployment_verification", revision)
                if (
                    self.verifier.accepts(task, deployment)
                    and prior
                    and prior["status"] == "succeeded"
                ):
                    result = VerificationResult.model_validate(prior["outcome"])
                else:
                    with self._operation(task, "deployment_verification", revision) as outcome:
                        result = await self.verifier.verify(task, deployment, worktree)
                        outcome.update(result.model_dump(mode="json"))
                self.storage.write_artifact(
                    task_id, "artifacts/playwright/output.txt", result.output or result.reason or ""
                )
                await self.github.publish_verification(task, result)
                if result.passed:
                    return self.storage.transition(
                        task_id, TaskState.WAITING_FOR_REVIEW, playwright_status="passed"
                    )
                attempts = task.attempts + 1
                if attempts >= self.config.model.max_task_iterations:
                    return self.storage.transition(
                        task_id,
                        TaskState.BLOCKED,
                        attempts=attempts,
                        playwright_status="failed",
                        error=result.reason,
                    )
                return self.storage.transition(
                    task_id,
                    TaskState.REPRODUCING,
                    attempts=attempts,
                    playwright_status="failed",
                )

            if task.state == TaskState.WAITING_FOR_REVIEW and self._has_review_comments(task_id):
                comments = self._take_review_comments(task_id)
                result = await self.agent.address_review(task, comments, worktree)
                if result.changed:
                    if not result.tests_passed:
                        attempts = task.attempts + 1
                        error = "agent reported that review-change tests did not pass"
                        if attempts >= self.config.model.max_task_iterations:
                            return self.storage.transition(
                                task_id,
                                TaskState.BLOCKED,
                                attempts=attempts,
                                error=error,
                            )
                        self._queue_review_comments(task_id, comments)
                        await self.wake(task_id)
                        return self.storage.transition(
                            task_id,
                            TaskState.WAITING_FOR_REVIEW,
                            attempts=attempts,
                            error=error,
                        )
                    return self.storage.transition(
                        task_id,
                        TaskState.IMPLEMENTING,
                        pr_head_sha=result.head_sha,
                        error=None,
                    )
            return task
        except RepositoryBusyError:
            # Contention is expected when several incidents target the same repository.
            # The worker will requeue this active task after yielding to the lock owner.
            await asyncio.sleep(max(1.0, self.config.poll_interval_seconds))
            return self.storage.load_task(task_id)
        except Exception as error:
            latest = self.storage.load_task(task_id)
            if latest.state in TERMINAL_STATES:
                return latest
            attempts = latest.attempts + 1
            self.storage.append_event(
                task_id,
                TaskEvent(type="task.error", data={"error": str(error), "attempt": attempts}),
            )
            if attempts >= self.config.model.max_task_iterations:
                return self.storage.transition(
                    task_id, TaskState.FAILED, attempts=attempts, error=str(error)
                )
            latest.attempts = attempts
            latest.error = str(error)
            self.storage.save_task(latest)
            return latest

    async def handle_github_event(self, event: str, payload: dict[str, Any]) -> TaskRecord | None:
        target = self.github.repository_and_pr(payload)
        task = self.storage.find_by_pr(*target) if target else None
        if task is None and target:
            candidates = []
            for candidate in self.storage.list_tasks("pending", "active", "waiting"):
                if not self._is_application(candidate):
                    continue
                for repository in self.application.repositories(candidate):
                    if (
                        repository.casefold() == target[0].casefold()
                        and application_value(candidate, repository, "pr_number") == target[1]
                    ):
                        candidates.append(candidate)
                        break
            if len(candidates) == 1:
                task = candidates[0]
        deployment_data = payload.get("deployment", payload)
        deployment_status = payload.get("deployment_status", {})
        deployment_repository = str(
            payload.get("repository", {}).get("full_name")
            or deployment_data.get("repository", {}).get("full_name", "")
        )
        deployment_sha = str(deployment_data.get("sha", ""))
        deployment_environment = str(deployment_data.get("environment", ""))
        # GitHub deployment_status events often have no pull_request object. Match
        # an application member only by its repository, exact current PR SHA, and
        # configured environment; ambiguity is rejected closed.
        if (
            task is None
            and event in {"deployment_status", "deployment"}
            and deployment_repository
            and deployment_sha
        ):
            matches: list[TaskRecord] = []
            for candidate in self.storage.list_tasks("pending", "active", "waiting"):
                if not self._is_application(candidate):
                    continue
                for repository in self.application.repositories(candidate):
                    if repository.casefold() != deployment_repository.casefold():
                        continue
                    if application_value(candidate, repository, "pr_head_sha") != deployment_sha:
                        continue
                    try:
                        expected = self.config.repository(repository).verification_environment
                    except KeyError:
                        continue
                    if deployment_environment == expected:
                        matches.append(candidate)
                        break
            if len(matches) == 1:
                task = matches[0]
        if not task:
            return None
        if task.state in TERMINAL_STATES:
            return task
        action = payload.get("action")
        if event == "pull_request" and action == "closed" and payload["pull_request"].get("merged"):
            if self._is_application(task):
                repository = (
                    target[0] if target else str(payload.get("repository", {}).get("full_name", ""))
                )
                update_application_state(task, repository, merged=True)
                self.storage.append_event(
                    task.task_id,
                    TaskEvent(
                        type="publication.merged",
                        data={"repository": repository, "pr_number": target[1] if target else None},
                    ),
                )
                self.storage.save_task(task)
                if self.application.all_deployments_verified(task):
                    return await self.application.maybe_complete(task)
                return task
            completed = self.storage.transition(task.task_id, TaskState.COMPLETED)
            self.storage.remove_worktree(completed)
            return completed
        if event in {"pull_request_review_comment", "issue_comment", "pull_request_review"}:
            comment = self.github.review_comment(payload)
            if comment:
                self._queue_review_comments(task.task_id, [comment], target[0] if target else None)
                await self.wake(task.task_id)
            return task
        if event in {"deployment_status", "deployment"}:
            status = deployment_status
            deployment = DeploymentReference(
                repository=deployment_repository or task.repository,
                environment=str(deployment_data.get("environment", "")),
                sha=str(deployment_data.get("sha", "")),
                url=str(status.get("environment_url") or status.get("target_url") or ""),
                deployment_id=deployment_data.get("id"),
                state=str(status.get("state", deployment_data.get("state", ""))),
            )
            if self._is_application(task):
                repository = next(
                    (
                        name
                        for name in self.application.repositories(task)
                        if name.casefold() == deployment.repository.casefold()
                    ),
                    deployment.repository,
                )
                if repository not in self.application.repositories(
                    task
                ) or not self.application.accept_deployment(task, repository, deployment):
                    return task
                task = self.application.record_deployment(task, repository, deployment)
                if task.state == TaskState.WAITING_FOR_DEPLOYMENT:
                    task = self.storage.transition(task.task_id, TaskState.TESTING_DEPLOYMENT)
                await self.wake(task.task_id)
                return task
            if task.state != TaskState.WAITING_FOR_DEPLOYMENT:
                return task
            if self.verifier.accepts(task, deployment):
                task = self.storage.transition(
                    task.task_id,
                    TaskState.TESTING_DEPLOYMENT,
                    deployment_environment=deployment.environment,
                    deployment_sha=deployment.sha,
                    deployment_url=deployment.url,
                )
                await self.wake(task.task_id)
            return task
        return task

    async def recover(self) -> None:
        for task in self.storage.list_tasks("pending", "active", "waiting"):
            if task.task_id in self._running_task_ids:
                continue
            application_comments = self._is_application(task) and any(
                application_value(task, repository, "pending_review_comments", [])
                for repository in self.application.repositories(task)
            )
            if task.state in ACTIVE_STATES or (
                task.state == TaskState.WAITING_FOR_REVIEW
                and (task.pending_review_comments or application_comments)
            ):
                await self.wake(task.task_id)

    async def run_worker(self) -> None:
        await self.recover()
        semaphore = asyncio.Semaphore(self.config.max_concurrent_tasks)

        async def run_one(task_id: str) -> None:
            task: TaskRecord | None = None
            try:
                async with semaphore:
                    task = await self.process(task_id)
            except Exception:
                logging.getLogger(__name__).exception("Task worker interrupted: %s", task_id)
            finally:
                self._running_task_ids.discard(task_id)
                replay = task_id in self._deferred_wakeups
                self._deferred_wakeups.discard(task_id)
                if not self._stopping.is_set() and (
                    replay or (task is not None and task.state in ACTIVE_STATES)
                ):
                    await self.wake(task_id)

        running: set[asyncio.Task[None]] = set()
        next_recovery = asyncio.get_running_loop().time() + self.config.poll_interval_seconds
        try:
            while not self._stopping.is_set():
                if asyncio.get_running_loop().time() >= next_recovery:
                    await self.recover()
                    next_recovery = (
                        asyncio.get_running_loop().time() + self.config.poll_interval_seconds
                    )
                if self.reload_model:
                    try:
                        self.reload_model()
                    except (OSError, ValueError, KeyError):
                        logging.getLogger(__name__).warning(
                            "Model configuration reload failed; retaining the last valid settings"
                        )
                try:
                    task_id = await asyncio.wait_for(
                        self._wakeups.get(),
                        timeout=max(0.001, next_recovery - asyncio.get_running_loop().time()),
                    )
                except TimeoutError:
                    continue
                self._queued_task_ids.discard(task_id)
                if self._stopping.is_set():
                    break
                self._running_task_ids.add(task_id)
                job = asyncio.create_task(run_one(task_id))
                running.add(job)
                job.add_done_callback(running.discard)
        except BaseException:
            for job in running:
                job.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            raise
        if running:
            await asyncio.gather(*running, return_exceptions=True)

    def stop(self) -> None:
        self._stopping.set()
