"""Shared, serialisable domain models for the incident harness."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_task_id() -> str:
    """Return a sortable-enough, dependency-free task identifier."""
    return f"{int(utc_now().timestamp() * 1000):013x}{uuid4().hex[:13]}".upper()


class TaskState(StrEnum):
    RECEIVED = "received"
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
    repository: str
    environment: str
    summary: str
    description: str = ""
    evidence: list[IncidentEvidence] = Field(default_factory=list)
    received_at: datetime = Field(default_factory=utc_now)


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
    agent_session_id: str | None = None
    backend_session_id: str | None = None
    repository: str
    environment: str
    summary: str
    branch: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    pr_head_sha: str | None = None
    deployment_environment: str | None = None
    deployment_sha: str | None = None
    deployment_url: str | None = None
    playwright_status: str | None = None
    attempts: int = 0
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


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
