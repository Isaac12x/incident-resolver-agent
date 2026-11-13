"""Installed package version information for the command line."""

from __future__ import annotations

import json
from importlib import metadata
from typing import Any

PACKAGE_NAME = "incident-harness"


def get_version_info() -> dict[str, str | None]:
    """Return installed package version and its VCS commit when available."""
    try:
        distribution = metadata.distribution(PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return {"name": PACKAGE_NAME, "version": "unknown", "commit": None}

    commit: str | None = None
    try:
        direct_url = distribution.read_text("direct_url.json")
    except (OSError, UnicodeDecodeError):
        direct_url = None
    if direct_url:
        try:
            details: Any = json.loads(direct_url)
        except (json.JSONDecodeError, UnicodeDecodeError):
            details = None
        if isinstance(details, dict):
            vcs_info = details.get("vcs_info")
            if isinstance(vcs_info, dict):
                commit_id = vcs_info.get("commit_id")
                if isinstance(commit_id, str) and commit_id:
                    commit = commit_id

    return {
        "name": PACKAGE_NAME,
        "version": distribution.version,
        "commit": commit,
    }
