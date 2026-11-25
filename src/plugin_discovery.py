"""Discover installed extension registrars once during application startup."""

from __future__ import annotations

import logging
from enum import Enum, auto
from importlib.metadata import entry_points
from threading import RLock

from .source_control import registration_transaction

logger = logging.getLogger(__name__)

_lock = RLock()
_discovered = False
_discovering = False


class DiscoveryState(Enum):
    """Outcome of process-wide plug-in discovery."""

    COMPLETE = auto()
    FAILED = auto()
    IN_PROGRESS = auto()


def discover_plugins() -> DiscoveryState:
    """Load and run installed ``incident_harness.plugins`` zero-argument registrars.

    Discovery is process-wide and serialized so repeated and concurrent application builds do
    not register extensions more than once. Each registrar is isolated and failed registrations
    are rolled back, allowing later extensions and core startup to continue.

    Returns:
        Whether discovery completed, failed to enumerate metadata, or is already in progress.

    Side effects:
        Loads installed entry points, may register providers and routes, and logs failures.
    """
    global _discovered, _discovering

    with _lock:
        if _discovered:
            return DiscoveryState.COMPLETE
        if _discovering:
            return DiscoveryState.IN_PROGRESS
        _discovering = True
        try:
            try:
                # Read the complete metadata index before running any registrar. If a
                # lazy index fails partway through, a later discovery retry cannot
                # invoke registrars that already ran during the failed enumeration.
                plugins = tuple(entry_points(group="incident_harness.plugins"))
            except Exception:
                logger.exception("failed to enumerate incident-harness plug-ins")
                return DiscoveryState.FAILED
            for plugin in plugins:
                plugin_name = plugin.name
                try:
                    with registration_transaction():
                        registrar = plugin.load()
                        if not callable(registrar):
                            raise TypeError(f"plug-in registrar is not callable: {plugin.name}")
                        registrar()
                except Exception:
                    logger.exception(
                        "failed to load incident-harness plug-in %s; skipping", plugin_name
                    )

            _discovered = True
            return DiscoveryState.COMPLETE
        finally:
            _discovering = False
