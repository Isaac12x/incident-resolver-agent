"""Extension points for model providers."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import contextmanager
from threading import RLock
from typing import TYPE_CHECKING

from .config import ModelConfig

if TYPE_CHECKING:
    from agents import Model

ModelProviderFactory = Callable[[ModelConfig], "Model"]
_model_provider_factories: dict[str, ModelProviderFactory] = {}
_model_provider_lock = RLock()


@contextmanager
def registration_transaction():
    """Make one extension's model-provider registrations atomic."""
    with _model_provider_lock:
        providers = _model_provider_factories.copy()
        try:
            yield
        except Exception:
            _model_provider_factories.clear()
            _model_provider_factories.update(providers)
            raise


def register_model_provider(name: str, factory: ModelProviderFactory) -> None:
    """Register a factory that builds an Agents SDK model for a provider name."""
    normalized = name.strip().casefold()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", normalized):
        raise ValueError("provider name must be a non-empty provider name")
    if not callable(factory):
        raise TypeError("model provider factory must be callable")
    with _model_provider_lock:
        existing = _model_provider_factories.get(normalized)
        if existing is factory:
            return
        if existing is not None:
            raise ValueError(f"model provider is already registered: {normalized}")
        _model_provider_factories[normalized] = factory


def get_model_provider(name: str) -> ModelProviderFactory | None:
    """Return the registered factory for a provider name, if present."""
    with _model_provider_lock:
        return _model_provider_factories.get(name.strip().casefold())
