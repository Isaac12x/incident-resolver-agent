"""GitHub authentication, event normalisation, and injectable API operations."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import subprocess
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from .config import Config, GitHubConfig
from .models import PullRequestReference, ReviewComment, TaskRecord, VerificationResult
from .storage import Storage


class WebhookSignatureError(PermissionError):
    pass


class GitHubCLIAdapter:
    """Publish with the same gh account used by repository selection in the TUI."""

    def __init__(self, config: Config, storage: Storage) -> None:
        self.config = config
        self.storage = storage

    @staticmethod
    async def _health_command(*command: str) -> str:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=4)
        except (TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            # gh/git stderr may echo credential-bearing remote URLs.
            raise RuntimeError(f"{command[0]} connection/access check failed")
        return stdout.decode()

    async def check_health(self) -> dict[str, dict[str, str]]:
        async def check(repository):
            key = f"github:{repository.name}"
            try:
                async with asyncio.timeout(5):
                    repo_json, _, _ = await asyncio.gather(
                        self._health_command(
                            "gh", "api", "--hostname", "github.com", f"repos/{repository.name}"
                        ),
                        self._health_command(
                            "gh", "api", "--hostname", "github.com",
                            f"repos/{repository.name}/pulls?per_page=1",
                        ),
                        self._health_command(
                            "git", "ls-remote", "--exit-code",
                            repository.clone_url or f"https://github.com/{repository.name}.git",
                            f"refs/heads/{repository.base_branch}",
                        ),
                    )
                    data = json.loads(repo_json)
                    if data.get("archived") or not data.get("permissions", {}).get("push"):
                        raise ValueError("repository archived or GitHub account lacks push access")
                return key, {"status": "ok"}
            except Exception as error:
                message = str(error) if isinstance(error, (RuntimeError, ValueError)) else (
                    "GitHub connection check timed out or could not run"
                )
                return key, {"status": "failed", "error": message}

        repositories = [r for r in self.config.repositories if r.publish_mode != "local" and (
            r.publish_mode == "github" or "github.com" in (r.clone_url or "")
        )]
        return dict(await asyncio.gather(*(check(r) for r in repositories)))

    def _api(self, endpoint: str, method: str = "GET", data: dict | None = None) -> Any:
        command = ["gh", "api", "--hostname", "github.com", endpoint, "--method", method]
        if data is not None:
            command.extend(["--input", "-"])
        result = subprocess.run(
            command,
            input=json.dumps(data) if data is not None else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if result.returncode:
            raise RuntimeError(f"GitHub {method} {endpoint} failed: {result.stderr.strip()}")
        return json.loads(result.stdout) if result.stdout.strip() else None

    @staticmethod
    def _git(worktree: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(worktree), *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if result.returncode:
            raise RuntimeError(f"Git {arguments[0]} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def _body(self, task: TaskRecord) -> str:
        directory = self.storage.task_directory(task.task_id)
        sections = [f"Fix incident {task.external_id}: {task.summary}"]
        for title, relative in (
            ("Root cause and evidence", "investigation.md"),
            ("Changed behavior and local verification", "artifacts/local/fix.txt"),
        ):
            path = directory / relative
            if path.exists():
                sections.append(f"## {title}\n\n{path.read_text(encoding='utf-8').strip()}")
        tests = [
            event.data
            for event in self.storage.events(task.task_id)
            if event.type == "verification.local"
        ]
        if tests:
            sections.append(
                "## Executed checks\n\n"
                + "\n".join(
                    f"- `{test['command']}`: {'passed' if test['passed'] else 'failed'}"
                    for test in tests
                )
            )
        sections.append(
            "## Deployment verification\n\nPending verification of this PR's exact head."
        )
        return "\n\n".join(sections)

    def _publish(self, task: TaskRecord) -> dict[str, Any]:
        if not task.branch:
            raise RuntimeError("cannot publish without an incident branch")
        repository = self.config.repository(task.repository)
        worktree = self.storage.root / "worktrees" / task.task_id
        # Do not create another commit when retrying a push or PR API failure.
        changes = self._git(
            worktree,
            "status",
            "--porcelain",
            "--",
            ".",
            ":(exclude)harness-out",
            ":(exclude).code-review-graph",
            ":(exclude).code-review-graph.db",
        )
        if changes:
            self.storage.commit_worktree(task, f"Fix incident {task.external_id}: {task.summary}")
        sha = self._git(worktree, "rev-parse", "HEAD")
        # A managed mirror has remote.origin.mirror=true; an incident must push only its branch.
        self._git(
            worktree,
            "-c",
            "remote.origin.mirror=false",
            "push",
            "origin",
            f"HEAD:refs/heads/{task.branch}",
        )
        endpoint = f"repos/{task.repository}/pulls"
        if task.pr_number:
            pull = self._api(f"{endpoint}/{task.pr_number}")
        else:
            from urllib.parse import urlencode

            query = urlencode(
                {
                    "state": "open",
                    "head": f"{task.repository.split('/')[0]}:{task.branch}",
                    "base": repository.base_branch,
                }
            )
            existing = self._api(f"{endpoint}?{query}")
            pull = (
                existing[0]
                if existing
                else self._api(
                    endpoint,
                    "POST",
                    {
                        "title": f"fix: {task.summary}"[:256],
                        "body": self._body(task),
                        "head": task.branch,
                        "base": repository.base_branch,
                        "draft": self.config.github.draft_pull_requests,
                    },
                )
            )
        if pull["head"]["sha"] != sha:
            raise RuntimeError("GitHub has not confirmed the pushed PR head; retry publication")
        reference = PullRequestReference(
            repository=task.repository,
            number=pull["number"],
            url=pull["html_url"],
            head_sha=sha,
            branch=task.branch,
        )
        self.storage._json_write(
            self.storage.task_directory(task.task_id) / "pr.json", reference.model_dump(mode="json")
        )
        return reference.model_dump(mode="json")

    def _operate(self, operation: str, payload: dict[str, Any]) -> Any:
        if operation in {"create_pull_request", "update_pull_request"}:
            return self._publish(TaskRecord.model_validate(payload))
        if operation == "publish_verification":
            task = TaskRecord.model_validate(payload["task"])
            result = VerificationResult.model_validate(payload["result"])
            return self._api(
                f"repos/{task.repository}/statuses/{result.sha}",
                "POST",
                {
                    "state": "success" if result.passed else "failure",
                    "context": "incident-harness/preview",
                    "description": (
                        result.reason
                        or "Preview verification " + ("passed" if result.passed else "failed")
                    )[:140],
                    "target_url": result.url,
                },
            )
        raise ValueError(f"unsupported GitHub operation: {operation}")

    async def __call__(self, operation: str, payload: dict[str, Any]) -> Any:
        return await asyncio.to_thread(self._operate, operation, payload)


class GitHubService:
    def __init__(
        self,
        config: GitHubConfig,
        *,
        webhook_secret: str | None = None,
        api: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None,
    ) -> None:
        self.config = config
        self.webhook_secret = webhook_secret
        self.api = api
        self._deliveries: deque[str] = deque(maxlen=10_000)
        self._delivery_set: set[str] = set()

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> None:
        if not self.webhook_secret:
            raise WebhookSignatureError("GitHub webhook secret is not configured")
        provided = headers.get("x-hub-signature-256", "")
        expected = (
            "sha256=" + hmac.new(self.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        )
        if not hmac.compare_digest(provided, expected):
            raise WebhookSignatureError("invalid GitHub webhook signature")

    def accept_delivery(self, delivery_id: str) -> bool:
        if not delivery_id or delivery_id in self._delivery_set:
            return False
        if len(self._deliveries) == self._deliveries.maxlen:
            self._delivery_set.discard(self._deliveries[0])
        self._deliveries.append(delivery_id)
        self._delivery_set.add(delivery_id)
        return True

    def review_comment(self, payload: dict[str, Any]) -> ReviewComment | None:
        comment = payload.get("comment") or payload.get("review", {})
        user = comment.get("user", {})
        login = str(user.get("login", ""))
        association = str(comment.get("author_association", "NONE")).upper()
        body = str(comment.get("body", ""))
        is_agent = login.casefold() == self.config.agent_login.casefold()
        if is_agent or association not in self.config.allowed_author_associations:
            return None
        # Review comments are actionable; issue comments must explicitly mention the agent.
        if "issue" in payload and self.config.agent_mention.casefold() not in body.casefold():
            return None
        return ReviewComment(
            id=int(comment.get("id", 0)),
            body=body,
            author=login,
            author_association=association,
            path=comment.get("path"),
            line=comment.get("line"),
            url=comment.get("html_url"),
        )

    async def _call(self, operation: str, payload: dict[str, Any]) -> Any:
        if not self.api:
            raise RuntimeError("GitHub API adapter is not configured")
        return await self.api(operation, payload)

    async def create_pull_request(self, task: TaskRecord) -> PullRequestReference:
        result = await self._call("create_pull_request", task.model_dump(mode="json"))
        return PullRequestReference.model_validate(result)

    async def update_pull_request(self, task: TaskRecord) -> PullRequestReference | None:
        """Notify the configured adapter that an existing PR received a new pushed head."""
        if not self.api:
            return None
        result = await self._call("update_pull_request", task.model_dump(mode="json"))
        return PullRequestReference.model_validate(result) if result else None

    async def get_review_threads(self, task: TaskRecord) -> list[ReviewComment]:
        result = await self._call("get_review_threads", task.model_dump(mode="json"))
        return [ReviewComment.model_validate(comment) for comment in result]

    async def publish_verification(self, task: TaskRecord, result: VerificationResult) -> None:
        await self._call(
            "publish_verification",
            {"task": task.model_dump(mode="json"), "result": result.model_dump(mode="json")},
        )

    async def reply_to_review(self, comment: ReviewComment, message: str) -> None:
        await self._call("reply_to_review", {"comment_id": comment.id, "message": message})

    @staticmethod
    def repository_and_pr(payload: dict[str, Any]) -> tuple[str, int] | None:
        repository = payload.get("repository", {}).get("full_name")
        pull_request = payload.get("pull_request") or payload.get("issue")
        number = pull_request.get("number") if isinstance(pull_request, dict) else None
        if repository and number:
            return str(repository), int(number)
        return None

    @staticmethod
    def decode(body: bytes) -> dict[str, Any]:
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("GitHub payload must be an object")
        return value
