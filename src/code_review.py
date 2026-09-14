"""Open Code Review execution and secret-free TUI provisioning."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import Config
from .tooling import CommandRunner


def executable(config: Config) -> str:
    local = config.runtime_root.resolve() / "tools/ocr/bin/ocr"
    return str(local) if local.is_file() else shutil.which("ocr") or "ocr"


def environment(config: Config) -> dict[str, str]:
    settings = config.code_review
    token = os.environ.get(settings.api_key_env, "")
    if not token:
        raise ValueError(f"OCR credential environment variable is missing: {settings.api_key_env}")
    env = {key: value for key, value in os.environ.items() if not key.startswith("OCR_LLM_")}
    env.update(
        OCR_LLM_URL=settings.base_url,
        OCR_LLM_MODEL=settings.model,
        OCR_LLM_PROTOCOL=settings.protocol,
        OCR_LLM_TOKEN=token,
    )
    return env


def provision(config: Config, runner: CommandRunner = subprocess.run) -> str:
    """Validate saved settings before installing, then test the configured endpoint."""
    Config.model_validate(config.model_dump())
    if not config.code_review.enabled:
        raise ValueError("Enable Open Code Review before setup")
    env = environment(config)
    binary = executable(config)
    if binary == "ocr":
        if not config.permissions.allow_dependency_installation:
            raise ValueError("Dependency installation is disabled")
        prefix = config.runtime_root.resolve() / "tools/ocr"
        result = runner(
            [
                "npm",
                "install",
                "--prefix",
                str(prefix),
                "--global",
                "@alibaba-group/open-code-review",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        if result.returncode:
            raise RuntimeError("OCR installation failed; check npm and network access")
        binary = str(prefix / "bin/ocr")
    result = runner(
        [binary, "llm", "test"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=config.code_review.timeout_seconds,
    )
    if result.returncode:
        raise RuntimeError("OCR connection test failed; check the configured endpoint and key")
    return "Open Code Review is installed and its model connection passed."


def review(config: Config, worktree: Path, base: str, branch: str, output: Path) -> dict[str, Any]:
    """Run the branch review, preserving JSON but never accepting incomplete results."""
    env = environment(config)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Each invocation gets a fresh path so old output cannot satisfy a failed run.
    output.unlink(missing_ok=True)
    result = subprocess.run(
        [
            executable(config),
            "review",
            "--from",
            base,
            "--to",
            branch,
            "--format",
            "json",
            "--output",
            str(output.resolve()),
        ],
        cwd=worktree,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=config.code_review.timeout_seconds,
    )
    if output.exists():
        content = output.read_text(encoding="utf-8").replace(env["OCR_LLM_TOKEN"], "[REDACTED]")
        output.write_text(content, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"OCR review failed (exit {result.returncode})")
    report = json.loads(output.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or report.get("status") not in {"success", "complete"}:
        raise ValueError("OCR did not return a complete review")
    if "comments" in report and report["comments"] is None:
        report["comments"] = []
    if not isinstance(report.get("comments"), list):
        raise ValueError("OCR report has no comments array")
    summary = report.get("summary") or {}
    if not isinstance(summary, dict):
        raise ValueError("OCR report has an invalid summary")
    if report.get("warnings") or summary.get("budget_exceeded"):
        raise ValueError("OCR review contains warnings or exhausted its budget")
    return report
