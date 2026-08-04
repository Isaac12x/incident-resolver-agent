"""GitHub authentication, event normalisation, and injectable API operations."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .config import GitHubConfig
from .models import PullRequestReference, ReviewComment, TaskRecord, VerificationResult


class WebhookSignatureError(PermissionError):
    pass


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
