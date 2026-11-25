"""Discover installed extension registrars once during application startup."""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from threading import RLock

_lock = RLock()
_discovered = False
_failure: Exception | None = None


def discover_plugins() -> None:
    """Load and run installed ``incident_harness.plugins`` zero-argument registrars.

    Discovery is process-wide and serialized so repeated and concurrent application builds do
    not register extensions more than once. A failed registrar leaves startup failed for the
    lifetime of the process; retrying a partially completed registrar could duplicate its work.
    """
    global _discovered, _failure

    with _lock:
        if _failure is not None:
            raise RuntimeError("installed plug-in discovery previously failed") from _failure
        if _discovered:
            return

        plugin_name = "<entry-point discovery>"
        try:
            for plugin in entry_points(group="incident_harness.plugins"):
                plugin_name = plugin.name
                registrar = plugin.load()
                if not callable(registrar):
                    raise TypeError(f"plug-in registrar is not callable: {plugin.name}")
                registrar()
        except Exception as error:
            _failure = error
            logging.getLogger(__name__).exception(
                "failed to load incident-harness plug-in %s", plugin_name
            )
            raise RuntimeError(
                f"failed to load incident-harness plug-in {plugin_name!r}"
            ) from error

        _discovered = True
