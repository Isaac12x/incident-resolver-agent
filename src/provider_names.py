"""Shared validation for source-control provider names."""

from __future__ import annotations

import re


def normalize_provider_name(name: str) -> str:
    """Normalize and validate a provider registration name."""
    normalized = name.strip().casefold()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", normalized):
        raise ValueError("provider name must be a non-empty provider name")
    return normalized
