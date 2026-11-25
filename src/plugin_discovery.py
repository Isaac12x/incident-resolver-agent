"""Discover installed extension registrars once during application startup."""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from threading import RLock

from .model_providers import registration_transaction as model_registration_transaction
from .source_control import registration_transaction

_lock = RLock()
_discovered = False
_discovering = False
_discovery_error: tuple[str, Exception] | None = None


def discover_plugins(*, strict: bool = False) -> None:
    """Load and run installed ``incident_harness.plugins`` zero-argument registrars.

    Discovery is process-wide and serialized so repeated and concurrent application builds do
    not register extensions more than once. Each registrar is isolated and failed registrations
    are rolled back, allowing later extensions and core startup to continue. Strict discovery
    reports the first registrar failure after completing and caching the pass.
    """
    global _discovered, _discovering, _discovery_error

    with _lock:
        if _discovering:
            return
        if _discovered:
            if strict and _discovery_error:
                name, error = _discovery_error
                raise RuntimeError(f"failed to load incident-harness plug-in {name}") from error
            return
        if strict:
            _discovery_error = None
        _discovering = True
        try:
            try:
                plugins = entry_points(group="incident_harness.plugins")
            except Exception:
                logging.getLogger(__name__).exception(
                    "failed to enumerate incident-harness plug-ins"
                )
                if strict:
                    raise
                return
            for plugin in plugins:
                plugin_name = plugin.name
                try:
                    with registration_transaction(), model_registration_transaction():
                        registrar = plugin.load()
                        if not callable(registrar):
                            raise TypeError(f"plug-in registrar is not callable: {plugin.name}")
                        registrar()
                except Exception as error:
                    logging.getLogger(__name__).exception(
                        "failed to load incident-harness plug-in %s; skipping", plugin_name
                    )
                    if _discovery_error is None:
                        _discovery_error = (plugin_name, error)

            _discovered = True
            if strict and _discovery_error:
                name, error = _discovery_error
                raise RuntimeError(f"failed to load incident-harness plug-in {name}") from error
        finally:
            _discovering = False
