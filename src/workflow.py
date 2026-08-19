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
from .tools import WorkspaceTools
from .verify import DeploymentVerifier

GraphIndexer = Callable[[Path], ToolResult]

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

    async def run_tests(self, command: str) -> dict[str, Any]:
        task = self._task()
        if task.state not in {
            TaskState.REPRODUCING,
            TaskState.IMPLEMENTING,
            TaskState.TESTING_LOCAL,
            TaskState.WAITING_FOR_REVIEW,
        }:
            raise RuntimeError(f"tests cannot run from {task.state.value}")
        if task.state != TaskState.IMPLEMENTING:
            task = self.workflow.storage.transition(self.task_id, TaskState.IMPLEMENTING)
        tools = WorkspaceTools(
            self.worktree,
            timeout=self.workflow.config.model.tool_timeout_seconds,
            permissions=self.workflow.config.permissions,
            logger=lambda data: self.workflow.storage.append_event(
                self.task_id, TaskEvent(type=str(data.pop("type")), data=data)
            ),
        )
        result = await tools.shell(command)
        passed = result.returncode == 0
        if passed and self.workflow.local_tester:
            passed = await self.workflow.local_tester(task, self.worktree)
        self.workflow.storage.append_event(
            self.task_id,
            TaskEvent(
                type="verification.local",
                data={
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
        }

    async def open_pr(self, summary: str) -> dict[str, Any]:
        task = self._task()
        if task.state not in {TaskState.TESTING_LOCAL, TaskState.PUBLISHING_PR}:
            raise RuntimeError("pull requests require successful local verification")
        self.workflow.storage.write_artifact(
            self.task_id, "artifacts/local/fix.txt", summary.strip()
        )
        self.workflow.storage.append_task_memory(
            self.task_id, f"## Verified fix\n\n{summary.strip()}\n"
        )
        repository = self.workflow.config.repository(task.repository)
        if task.pr_number:
            result = await WorkspaceTools(self.worktree).shell("git rev-parse HEAD")
            if result.returncode:
                raise RuntimeError(result.stderr or "could not determine updated PR head")
            if not task.branch:
                raise RuntimeError("cannot update a pull request without its branch")
            push = await WorkspaceTools(self.worktree).shell(
                f"git push origin HEAD:{task.branch}"
            )
            if push.returncode:
                raise RuntimeError(push.stderr or "could not push the updated pull-request head")
            updated = task.model_copy(update={"pr_head_sha": result.stdout.strip()})
            reference = await self.workflow.github.update_pull_request(updated)
            task = self.workflow.storage.transition(
                self.task_id,
                TaskState.WAITING_FOR_DEPLOYMENT,
                pr_head_sha=result.stdout.strip(),
                pr_url=reference.url if reference else task.pr_url,
            )
        elif repository.publish_mode == "local" or (
            repository.publish_mode == "auto" and self.workflow.github.api is None
        ):
            task = self.workflow._local_publish(task, self.worktree)
        else:
            if task.state != TaskState.PUBLISHING_PR:
                task = self.workflow.storage.transition(self.task_id, TaskState.PUBLISHING_PR)
            pull_request = await self.workflow.github.create_pull_request(task)
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
        self._wakeups: asyncio.Queue[str] = asyncio.Queue()
        self._stopping = asyncio.Event()
        self._review_comments: dict[str, list[ReviewComment]] = {}
        self._queued_task_ids: set[str] = set()
        self._running_task_ids: set[str] = set()
        self._deferred_wakeups: set[str] = set()

    def _has_review_comments(self, task_id: str) -> bool:
        task = self.storage.load_task(task_id)
        return bool(task.pending_review_comments or self._review_comments.get(task_id))

    def _take_review_comments(self, task_id: str) -> list[ReviewComment]:
        task = self.storage.load_task(task_id)
        comments = list(task.pending_review_comments)
        comments.extend(self._review_comments.pop(task_id, []))
        unique = {comment.id: comment for comment in comments}
        if task.pending_review_comments:
            task.pending_review_comments = []
            self.storage.save_task(task)
        return list(unique.values())

    def _queue_review_comments(self, task_id: str, comments: list[ReviewComment]) -> None:
        task = self.storage.load_task(task_id)
        known = {comment.id for comment in task.pending_review_comments}
        task.pending_review_comments.extend(
            comment for comment in comments if comment.id not in known
        )
        self.storage.save_task(task)
        self._review_comments[task_id] = list(task.pending_review_comments)

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
                detail = (
                    f"{result.command[0]} exited {result.returncode}: {result.stderr.strip()}"
                )
                raise RuntimeError(f"repository graph generation failed: {detail}")
            self.storage.write_artifact(
                task.task_id,
                "artifacts/repository/graphs-ready",
                "code-review-graph completed\n",
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

    async def _process_agent_session(self, task: TaskRecord, worktree: Path) -> TaskRecord:
        lifecycle = _TaskLifecycle(self, task.task_id, worktree)
        prompt: str | None = None
        if task.state == TaskState.COLLECTING_CONTEXT:
            context = self.storage.task_directory(task.task_id) / "context.md"
            if context.exists():
                prompt = (
                    "Resolve this incident end to end in the durable task session. The collected "
                    "incident context follows.\n\n" + context.read_text(encoding="utf-8")
                )
        elif task.state == TaskState.WAITING_FOR_REVIEW:
            comments = self._take_review_comments(task.task_id)
            prompt = "Address these authorized review comments in the same task session:\n\n" + (
                "\n".join(f"{comment.author}: {comment.body}" for comment in comments)
            )
        else:
            prompt = (
                f"Resume the same durable incident session from state `{task.state.value}`. "
                f"The last recorded error was: {task.error or 'none'}. Continue toward a verified "
                "pull request using lifecycle tools."
            )
        result = await self.agent.run_session(task, worktree, lifecycle, prompt)
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
            uses_durable_session = bool(
                getattr(self.agent, "supports_durable_session", False)
                and callable(getattr(self.agent, "run_session", None))
            )
            if uses_durable_session and (
                (
                    task.state in ACTIVE_STATES
                    and task.state != TaskState.TESTING_DEPLOYMENT
                )
                or (
                    task.state == TaskState.WAITING_FOR_REVIEW
                    and self._has_review_comments(task_id)
                )
            ):
                return await self._process_agent_session(task, worktree)
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
                self._queue_review_comments(task.task_id, [comment])
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
        for task in self.storage.list_tasks("pending", "active", "waiting"):
            if task.state in ACTIVE_STATES or task.pending_review_comments:
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
