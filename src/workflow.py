"""Filesystem-backed incident workflow and event routing."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .agent import IncidentAgent
from .config import Config, RepositoryConfig
from .github import GitHubService
from .models import (
    DeploymentReference,
    Incident,
    PullRequestReference,
    ReviewComment,
    TaskEvent,
    TaskRecord,
    TaskState,
)
from .storage import Storage
from .tooling import ToolResult
from .verify import DeploymentVerifier

GraphIndexer = Callable[[Path], tuple[ToolResult, ToolResult]]

ACTIVE_STATES = {
    TaskState.RECEIVED,
    TaskState.COLLECTING_CONTEXT,
    TaskState.INVESTIGATING,
    TaskState.REPRODUCING,
    TaskState.IMPLEMENTING,
    TaskState.TESTING_LOCAL,
    TaskState.PUBLISHING_PR,
    TaskState.TESTING_DEPLOYMENT,
}
TERMINAL_STATES = {
    TaskState.COMPLETED,
    TaskState.BLOCKED,
    TaskState.FAILED,
    TaskState.CANCELLED,
}


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
        self._wakeups: asyncio.Queue[str] = asyncio.Queue()
        self._stopping = asyncio.Event()
        self._review_comments: dict[str, list[ReviewComment]] = {}
        self._queued_task_ids: set[str] = set()
        self._running_task_ids: set[str] = set()
        self._deferred_wakeups: set[str] = set()

    async def submit(self, incident: Incident) -> TaskRecord:
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
        existing = self.storage.find_by_incident(incident.source, incident.external_id)
        task = self.storage.create_task(incident)
        if existing is None:
            await self.wake(task.task_id)
        return task

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
        graph_marker = (
            self.storage.task_directory(task.task_id) / "artifacts" / "repository" / "graphs-ready"
        )
        if self.repository_indexer and not graph_marker.exists():
            results = await asyncio.to_thread(self.repository_indexer, worktree)
            for name, result in zip(("graphify", "code-review-graph"), results, strict=True):
                self.storage.append_event(
                    task.task_id,
                    TaskEvent(
                        type="repository.graph_indexed",
                        data={
                            "tool": name,
                            "command": list(result.command),
                            "returncode": result.returncode,
                            "stderr": result.stderr[-2000:],
                        },
                    ),
                )
            failures = [result for result in results if not result.succeeded]
            if failures:
                detail = "; ".join(
                    f"{result.command[0]} exited {result.returncode}: {result.stderr.strip()}"
                    for result in failures
                )
                raise RuntimeError(f"repository graph generation failed: {detail}")
            self.storage.write_artifact(
                task.task_id,
                "artifacts/repository/graphs-ready",
                "graphify and code-review-graph completed\n",
            )
        return worktree

    def _local_publish(self, task: TaskRecord, worktree: Path) -> TaskRecord:
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

    async def process(self, task_id: str) -> TaskRecord:
        task = self.storage.load_task(task_id)
        try:
            if task.state == TaskState.RECEIVED:
                worktree = await self._worktree(task)
                context = (
                    await self.context_collector(task, worktree)
                    if self.context_collector
                    else self.storage.load_incident(task_id).model_dump_json(indent=2)
                )
                self.storage.write_artifact(task_id, "context.md", context)
                return self.storage.transition(task_id, TaskState.COLLECTING_CONTEXT)

            worktree = await self._worktree(task)
            if task.state == TaskState.COLLECTING_CONTEXT:
                investigation = await self.agent.investigate(task, worktree)
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
                repository = self.config.repository(task.repository)
                if repository.publish_mode == "local" or (
                    repository.publish_mode == "auto" and self.github.api is None
                ):
                    return self._local_publish(task, worktree)
                pull_request = await self.github.create_pull_request(task)
                return self.storage.transition(
                    task_id,
                    TaskState.WAITING_FOR_DEPLOYMENT,
                    branch=pull_request.branch,
                    pr_number=pull_request.number,
                    pr_url=pull_request.url,
                    pr_head_sha=pull_request.head_sha,
                )

            if task.state == TaskState.TESTING_DEPLOYMENT:
                deployment = DeploymentReference(
                    repository=task.repository,
                    environment=task.deployment_environment or "",
                    sha=task.deployment_sha or "",
                    url=task.deployment_url or "",
                )
                result = await self.verifier.verify(task, deployment, worktree)
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

            if task.state == TaskState.WAITING_FOR_REVIEW and self._review_comments.get(task_id):
                comments = self._review_comments.pop(task_id)
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
                        self._review_comments[task_id] = comments
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
        except Exception as error:
            latest = self.storage.load_task(task_id)
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
        if not target:
            return None
        task = self.storage.find_by_pr(*target)
        if not task:
            return None
        action = payload.get("action")
        if event == "pull_request" and action == "closed" and payload["pull_request"].get("merged"):
            completed = self.storage.transition(task.task_id, TaskState.COMPLETED)
            self.storage.remove_worktree(completed)
            return completed
        if event in {"pull_request_review_comment", "issue_comment", "pull_request_review"}:
            comment = self.github.review_comment(payload)
            if comment:
                self._review_comments.setdefault(task.task_id, []).append(comment)
                await self.wake(task.task_id)
            return task
        if event in {"deployment_status", "deployment"}:
            deployment_data = payload.get("deployment", payload)
            status = payload.get("deployment_status", {})
            deployment = DeploymentReference(
                repository=task.repository,
                environment=str(deployment_data.get("environment", "")),
                sha=str(deployment_data.get("sha", "")),
                url=str(status.get("environment_url") or status.get("target_url") or ""),
                deployment_id=deployment_data.get("id"),
                state=str(status.get("state", deployment_data.get("state", ""))),
            )
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
        for task in self.storage.list_tasks("pending", "active"):
            await self.wake(task.task_id)

    async def run_worker(self) -> None:
        await self.recover()
        semaphore = asyncio.Semaphore(self.config.max_concurrent_tasks)

        async def run_one(task_id: str) -> None:
            task: TaskRecord | None = None
            try:
                async with semaphore:
                    task = await self.process(task_id)
            finally:
                self._running_task_ids.discard(task_id)
                replay = task_id in self._deferred_wakeups
                self._deferred_wakeups.discard(task_id)
                if not self._stopping.is_set() and (
                    replay or (task is not None and task.state in ACTIVE_STATES)
                ):
                    await self.wake(task_id)

        running: set[asyncio.Task[None]] = set()
        while not self._stopping.is_set():
            try:
                task_id = await asyncio.wait_for(
                    self._wakeups.get(), timeout=self.config.poll_interval_seconds
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
        if running:
            await asyncio.gather(*running, return_exceptions=True)

    def stop(self) -> None:
        self._stopping.set()
