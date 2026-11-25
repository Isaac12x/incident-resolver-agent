"""Extension points for source-control providers and their webhook routes.

External packages register during application startup, before building the
application or server. The registries are empty by default, so core has no
optional plug-in dependency.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from fastapi import APIRouter, FastAPI

from .models import PullRequestReference, ReviewComment, TaskRecord, VerificationResult
from .provider_names import normalize_provider_name
from .storage import Storage

if TYPE_CHECKING:
    from .config import Config


class SourceControlProvider(Protocol):
    """Provider operations used by the workflow's publication lifecycle."""

    @property
    def publish_enabled(self) -> bool: ...

    async def create_pull_request(self, task: TaskRecord) -> PullRequestReference: ...

    async def update_pull_request(self, task: TaskRecord) -> PullRequestReference | None: ...

    async def publish_verification(self, task: TaskRecord, result: VerificationResult) -> None: ...

    def authenticate_and_translate(
        self, headers: Mapping[str, str], body: bytes
    ) -> tuple[str, dict[str, Any], bool]: ...

    def repository_and_pr(self, payload: dict[str, Any]) -> tuple[str, int] | None: ...

    def review_comment(self, payload: dict[str, Any]) -> ReviewComment | None: ...


@runtime_checkable
class ApplicationSyncProvider(Protocol):
    """Optional capability for refreshing grouped publication metadata."""

    async def sync_application_pull_requests(self, task: TaskRecord) -> None: ...


@runtime_checkable
class HealthCheckProvider(Protocol):
    """Optional capability for provider-specific connection checks."""

    async def check_health(self) -> dict[str, dict[str, str]]: ...


@runtime_checkable
class PullRequestPollingProvider(Protocol):
    """Optional capability for checking a pull request's current state."""

    async def get_pull_request(self, task: TaskRecord) -> dict[str, Any]: ...


@runtime_checkable
class LocalUpdateProvider(Protocol):
    """Optional capability for publishing an update from a local checkout."""

    async def update_from_worktree(
        self, task: TaskRecord, worktree: Path
    ) -> PullRequestReference: ...


ProviderFactory = Callable[["Config", Storage], SourceControlProvider]
_provider_factories: dict[str, ProviderFactory] = {}
_webhook_routers: list[APIRouter] = []
_registry_lock = RLock()


@contextmanager
def registration_transaction():
    """Make one registrar's provider and router registrations atomic."""
    with _registry_lock:
        providers = _provider_factories.copy()
        routers = _webhook_routers.copy()
        try:
            yield
        except Exception:
            _provider_factories.clear()
            _provider_factories.update(providers)
            _webhook_routers[:] = routers
            raise


def register_source_control_provider(name: str, factory: ProviderFactory) -> None:
    """Register a provider factory under the ``RepositoryConfig.publish_mode`` name."""
    normalized = normalize_provider_name(name)
    if normalized in {"auto", "local"}:
        raise ValueError("provider name cannot be 'auto' or 'local'")
    with _registry_lock:
        if normalized in _provider_factories:
            raise ValueError(f"source-control provider is already registered: {normalized}")
        _provider_factories[normalized] = factory


def create_source_control_provider(
    name: str, config: Config, storage: Storage
) -> SourceControlProvider:
    """Build a previously registered provider, passing core config and storage."""
    normalized = normalize_provider_name(name)
    with _registry_lock:
        try:
            factory = _provider_factories[normalized]
        except KeyError as error:
            raise ValueError(f"source-control provider is not registered: {normalized}") from error
    return factory(config, storage)


def register_webhook_router(router: APIRouter) -> None:
    """Mount an external provider's FastAPI routes on every core server."""
    if not isinstance(router, APIRouter):
        raise TypeError("webhook router must be a FastAPI APIRouter")
    with _registry_lock:
        if router not in _webhook_routers:
            _webhook_routers.append(router)


def mount_webhook_routers(server: FastAPI) -> None:
    """Include all routes registered by installed provider packages."""
    with _registry_lock:
        routers = tuple(_webhook_routers)
    for router in routers:
        server.include_router(router)
