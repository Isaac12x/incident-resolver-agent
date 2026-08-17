from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "remove-file-from-branches.py"
SPEC = importlib.util.spec_from_file_location("remove_file_from_branches", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_master_push_is_forced() -> None:
    assert module.push_command("master") == [
        "git",
        "push",
        "--force",
        "origin",
        "HEAD:refs/heads/master",
    ]


def test_non_master_push_is_not_forced() -> None:
    assert module.push_command("feature/remove-generated-file") == [
        "git",
        "push",
        "origin",
        "HEAD:refs/heads/feature/remove-generated-file",
    ]
