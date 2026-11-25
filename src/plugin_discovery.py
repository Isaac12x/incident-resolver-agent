"""Discover installed extension registrars once during application startup."""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from threading import RLock

from .source_control import registration_transaction

_lock = RLock()
_discovered = False


def discover_plugins() -> None:
    """Load and run installed ``incident_harness.plugins`` zero-argument registrars.

    Discovery is process-wide and serialized so repeated and concurrent application builds do
    not register extensions more than once. Each registrar is isolated and failed registrations
    are rolled back, allowing later extensions and core startup to continue.
    """
    global _discovered

    with _lock:
        if _discovered:
            return
        try:
            plugins = entry_points(group="incident_harness.plugins")
        except Exception:
            logging.getLogger(__name__).exception("failed to enumerate incident-harness plug-ins")
            _discovered = True
            return
        for plugin in plugins:
            plugin_name = plugin.name
            try:
                with registration_transaction():
                    registrar = plugin.load()
                    if not callable(registrar):
                        raise TypeError(f"plug-in registrar is not callable: {plugin.name}")
                    registrar()
            except Exception:
                logging.getLogger(__name__).exception(
                    "failed to load incident-harness plug-in %s; skipping", plugin_name
                )

        _discovered = True
