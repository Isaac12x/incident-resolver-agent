from __future__ import annotations

import sys

from src.subprocess_json import run_bounded_json


def test_bounded_json_success_and_nonzero() -> None:
    command = [
        sys.executable,
        "-c",
        "import json,sys; print(json.dumps({'ok':json.load(sys.stdin)['x']}))",
    ]
    assert run_bounded_json(command, {"x": 3})["result"] == {"ok": 3}
    failed = run_bounded_json(
        [sys.executable, "-c", "print('{\"ok\":true}'); raise SystemExit(2)"], {}
    )
    assert failed["available"] is False and failed["returncode"] == 2


def test_bounded_json_rejects_invalid_missing_and_large_output() -> None:
    invalid = run_bounded_json([sys.executable, "-c", "print('no')"], {})
    assert invalid["reason"] == "provider returned invalid JSON"
    missing = run_bounded_json(["definitely-not-a-provider"], {})
    assert missing["available"] is False
    large = run_bounded_json([sys.executable, "-c", "print('x' * 400)"], {}, max_bytes=256)
    assert large["reason"] == "provider output exceeded limit"


def test_bounded_json_timeout_includes_input_write() -> None:
    result = run_bounded_json(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        {"data": "x" * 1_000_000},
        timeout_seconds=1,
    )
    assert result == {"available": False, "reason": "TimeoutExpired"}


def test_bounded_json_validates_command_and_bounds() -> None:
    import pytest

    with pytest.raises(ValueError):
        run_bounded_json([], {})
    with pytest.raises(ValueError):
        run_bounded_json([sys.executable], {}, max_bytes=1)
