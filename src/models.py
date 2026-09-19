"""Shared, serialisable domain models for the incident harness."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_task_id() -> str:
    """Return a sortable-enough, dependency-free task identifier."""
    return f"{int(utc_now().timestamp() * 1000):013x}{uuid4().hex[:13]}".upper()


class TaskState(StrEnum):
    RECEIVED = "received"
    TRIAGING = "triaging"
    COLLECTING_CONTEXT = "collecting_context"
    INVESTIGATING = "investigating"
    REPRODUCING = "reproducing"
    IMPLEMENTING = "implementing"
    TESTING_LOCAL = "testing_local"
    PUBLISHING_PR = "publishing_pr"
    WAITING_FOR_DEPLOYMENT = "waiting_for_pr_deployment"
    TESTING_DEPLOYMENT = "testing_deployment"
    WAITING_FOR_REVIEW = "waiting_for_review"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class IncidentEvidence(BaseModel):
    kind: str
    content: str | None = None
    url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Incident(BaseModel):
    external_id: str
    source: str
    environment: str
    summary: str
    # Empty repository is intentional for application-scoped intake.  The
    # configured application membership supplies the target repositories.
    repository: str = ""
    application: str | None = None
    service: str | None = None
    description: str = ""
    evidence: list[IncidentEvidence] = Field(default_factory=list)
    received_at: datetime = Field(default_factory=utc_now)

    @field_validator("application", "service")
    @classmethod
    def optional_scope_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or any(character in value for character in "\r\n"):
            raise ValueError("scope names must be nonempty and single-line")
        return value

    @field_validator("environment", "external_id", "source", "summary")
    @classmethod
    def incident_string_fields(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("incident identity and summary fields cannot be blank")
        return value

    @field_validator("repository")
    @classmethod
    def optional_repository(cls, value: str) -> str:
        value = value.strip()
        if any(character in value for character in "\r\n"):
            raise ValueError("repository must be a single-line identifier")
        return value


class RepositoryTarget(BaseModel):
    name: str
    base_branch: str = "main"


class PullRequestReference(BaseModel):
    repository: str
    number: int
    url: str
    head_sha: str
    branch: str


class DeploymentReference(BaseModel):
    repository: str
    environment: str
    sha: str
    url: str
    deployment_id: int | str | None = None
    state: str = "success"


class VerificationResult(BaseModel):
    passed: bool
    environment: str
    sha: str
    url: str
    command: str | None = None
    output: str = ""
    reason: str | None = None
    verified_at: datetime = Field(default_factory=utc_now)


class ReviewComment(BaseModel):
    id: int
    body: str
    author: str
    author_association: str = "NONE"
    path: str | None = None
    line: int | None = None
    url: str | None = None
    is_agent: bool = False
    resolved: bool = False


class RepositoryTaskState(BaseModel):
    """Durable state for one repository in an application task."""

    repository: str
    state: TaskState = TaskState.RECEIVED
    changed: bool = False
    merged: bool = False
    base_sha: str | None = None
    branch: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    pr_head_sha: str | None = None
    deployment_environment: str | None = None
    deployment_sha: str | None = None
    deployment_url: str | None = None
    playwright_status: str | None = None
    code_review_sha: str | None = None
    verification_sha: str | None = None
    verification_status: str | None = None
    verification_command: str | None = None
    verification_output: str = ""
    pending_review_comments: list[ReviewComment] = Field(default_factory=list)
    verification_progress: dict[str, Any] = Field(default_factory=dict)
    attempts: int = 0
    conflict_recovery_attempts: int = 0
    conflict_base_branch: str | None = None
    conflict_pending: bool = False
    conflict_merge_pending: bool = False
    error: str | None = None


class TaskEvent(BaseModel):
    type: str
    time: datetime = Field(default_factory=utc_now)
    data: dict[str, Any] = Field(default_factory=dict)


class TaskRecord(BaseModel):
    task_id: str = Field(default_factory=new_task_id)
    state: TaskState = TaskState.RECEIVED
    external_id: str
    source: str
    conversation_id: str
    triage: dict[str, Any] | None = None
    agent_session_id: str | None = None
    backend_session_id: str | None = None
    pending_review_comments: list[ReviewComment] = Field(default_factory=list)
    repository: str
    environment: str
    summary: str
    application: str | None = None
    service: str | None = None
    repositories: dict[str, RepositoryTaskState] = Field(default_factory=dict)
    branch: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    pr_head_sha: str | None = None
    deployment_environment: str | None = None
    deployment_sha: str | None = None
    deployment_url: str | None = None
    playwright_status: str | None = None
    code_review_sha: str | None = None
    attempts: int = 0
    conflict_recovery_attempts: int = 0
    conflict_base_branch: str | None = None
    conflict_pending: bool = False
    conflict_merge_pending: bool = False
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def repository_names(self) -> list[str]:
        """Return the task's snapshotted targets, retaining legacy semantics."""
        return (
            list(self.repositories)
            if self.repositories
            else ([self.repository] if self.repository else [])
        )

    def repository_state(self, repository: str | None = None) -> RepositoryTaskState:
        """Project the compatibility fields or one application member."""
        name = repository or self.repository
        if self.repositories:
            for key, state in self.repositories.items():
                if key.casefold() == name.casefold():
                    return state
            raise KeyError(f"repository is not in task scope: {name}")
        return RepositoryTaskState(
            repository=name,
            changed=False,
            merged=False,
            branch=self.branch,
            pr_number=self.pr_number,
            pr_url=self.pr_url,
            pr_head_sha=self.pr_head_sha,
            deployment_environment=self.deployment_environment,
            deployment_sha=self.deployment_sha,
            deployment_url=self.deployment_url,
            playwright_status=self.playwright_status,
            code_review_sha=self.code_review_sha,
            pending_review_comments=list(self.pending_review_comments),
            attempts=self.attempts,
            conflict_recovery_attempts=self.conflict_recovery_attempts,
            conflict_base_branch=self.conflict_base_branch,
            conflict_pending=self.conflict_pending,
            conflict_merge_pending=self.conflict_merge_pending,
            error=self.error,
        )

    def for_repository(self, repository: str | None = None) -> TaskRecord:
        """Return a compatibility view containing one member's lifecycle fields."""
        state = self.repository_state(repository)
        return self.model_copy(
            update={
                "repository": state.repository,
                "branch": state.branch,
                "pr_number": state.pr_number,
                "pr_url": state.pr_url,
                "pr_head_sha": state.pr_head_sha,
                "deployment_environment": state.deployment_environment,
                "deployment_sha": state.deployment_sha,
                "deployment_url": state.deployment_url,
                "playwright_status": state.playwright_status,
                "code_review_sha": state.code_review_sha,
                "pending_review_comments": list(state.pending_review_comments),
            }
        )


class TaskResult(BaseModel):
    status: str
    summary: str
    root_cause: str | None = None
    pull_request: PullRequestReference | None = None
    verification: VerificationResult | None = None


class InvestigationResult(BaseModel):
    root_cause: str
    evidence: list[str] = Field(default_factory=list)
    proposed_fix: str
    reproducible: bool = False


class FixResult(BaseModel):
    changed: bool
    summary: str
    tests_passed: bool = False
    blocked_reason: str | None = None


class ReviewResult(BaseModel):
    changed: bool
    summary: str
    tests_passed: bool = False
    head_sha: str | None = None


class SessionResult(BaseModel):
    """A checkpoint emitted by a durable task session after it yields control."""

    summary: str
    waiting_for_external_event: bool = False
    blocked_reason: str | None = None
