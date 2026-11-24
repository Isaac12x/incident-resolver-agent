"""Extension points for source-control providers and their webhook routes.

External packages register during application startup, before building the
application or server. The registries are empty by default, so core has no
optional plug-in dependency.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Protocol

from fastapi import APIRouter

from .config import Config
from .models import PullRequestReference, ReviewComment, TaskRecord, VerificationResult
from .storage import Storage


class SourceControlProvider(Protocol):
    """Provider operations used by the workflow's publication lifecycle."""

    @property
    def publish_enabled(self) -> bool: ...

    async def create_pull_request(self, task: TaskRecord) -> PullRequestReference: ...

    async def update_pull_request(self, task: TaskRecord) -> PullRequestReference | None: ...

    async def publish_verification(self, task: TaskRecord, result: VerificationResult) -> None: ...

    def repository_and_pr(self, payload: dict[str, Any]) -> tuple[str, int] | None: ...

    def review_comment(self, payload: dict[str, Any]) -> ReviewComment | None: ...


ProviderFactory = Callable[[Config, Storage], SourceControlProvider]
_provider_factories: dict[str, ProviderFactory] = {}
_webhook_routers: list[APIRouter] = []


def register_source_control_provider(name: str, factory: ProviderFactory) -> None:
    """Register a provider factory under the ``RepositoryConfig.publish_mode`` name."""
    normalized = name.strip().casefold()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", normalized):
        raise ValueError("provider name must be a non-empty provider name")
    if normalized in {"auto", "local"}:
        raise ValueError("provider name cannot be 'auto' or 'local'")
    if normalized in _provider_factories:
        raise ValueError(f"source-control provider is already registered: {normalized}")
    _provider_factories[normalized] = factory


def create_source_control_provider(
    name: str, config: Config, storage: Storage
) -> SourceControlProvider:
    """Build a previously registered provider, passing core config and storage."""
    normalized = name.strip().casefold()
    try:
        factory = _provider_factories[normalized]
    except KeyError as error:
        raise ValueError(f"source-control provider is not registered: {normalized}") from error
    return factory(config, storage)


def registered_source_control_provider_names() -> tuple[str, ...]:
    """Return registered names for configuration editors and diagnostics."""
    return tuple(sorted(_provider_factories))


def register_webhook_router(router: APIRouter) -> None:
    """Mount an external provider's FastAPI routes on every core server."""
    if not isinstance(router, APIRouter):
        raise TypeError("webhook router must be a FastAPI APIRouter")
    _webhook_routers.append(router)


def mount_webhook_routers(server: Any) -> None:
    """Include all routes registered by installed provider packages."""
    for router in _webhook_routers:
        server.include_router(router)
