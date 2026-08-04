"""Strict matching and execution of deployment verification."""

from __future__ import annotations

import asyncio
import os
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from .config import Config
from .models import DeploymentReference, TaskRecord, VerificationResult


class DeploymentVerifier:
    def __init__(
        self,
        config: Config,
        *,
        deployment_source: (
            Callable[[TaskRecord], Awaitable[list[DeploymentReference]]] | None
        ) = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.deployment_source = deployment_source
        self.client = client

    def accepts(self, task: TaskRecord, deployment: DeploymentReference) -> bool:
        try:
            repository = self.config.repository(task.repository)
        except KeyError:
            return False
        return bool(
            task.pr_number
            and task.pr_head_sha
            and deployment.repository == task.repository
            and deployment.environment == repository.verification_environment
            and deployment.sha == task.pr_head_sha
            and deployment.state.casefold() in {"success", "ready", "active"}
            and deployment.url.startswith(("https://", "http://"))
        )

    async def find_current_deployment(self, task: TaskRecord) -> DeploymentReference | None:
        if not self.deployment_source:
            return None
        deployments = await self.deployment_source(task)
        return next((item for item in reversed(deployments) if self.accepts(task, item)), None)

    async def _reachable(self, url: str) -> bool:
        client = self.client or httpx.AsyncClient(follow_redirects=True)
        owns_client = self.client is None
        try:
            timeout = self.config.deployment.reachability_timeout_seconds
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                try:
                    response = await client.get(url, timeout=10)
                    if response.status_code < 500:
                        return True
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(self.config.deployment.poll_interval_seconds)
            return False
        finally:
            if owns_client:
                await client.aclose()

    async def verify(
        self, task: TaskRecord, deployment: DeploymentReference, worktree: Path
    ) -> VerificationResult:
        if not self.accepts(task, deployment):
            return VerificationResult(
                passed=False,
                environment=deployment.environment,
                sha=deployment.sha,
                url=deployment.url,
                reason="deployment does not match the current PR head and required environment",
            )
        if not await self._reachable(deployment.url):
            return VerificationResult(
                passed=False,
                environment=deployment.environment,
                sha=deployment.sha,
                url=deployment.url,
                reason="deployment URL did not become reachable",
            )
        repository = self.config.repository(task.repository)
        command = repository.playwright.command
        if not command:
            return VerificationResult(
                passed=False,
                environment=deployment.environment,
                sha=deployment.sha,
                url=deployment.url,
                reason="Playwright command is not configured",
            )
        environment = os.environ.copy()
        environment[repository.playwright.base_url_env] = deployment.url
        attempts = repository.playwright.retries + 1
        outputs: list[str] = []
        for attempt in range(1, attempts + 1):
            process = await asyncio.create_subprocess_exec(
                *shlex.split(command),
                cwd=worktree,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                output, _ = await asyncio.wait_for(
                    process.communicate(), timeout=repository.playwright.timeout_seconds
                )
            except TimeoutError:
                process.kill()
                await process.wait()
                outputs.append(f"Attempt {attempt}/{attempts}: timed out")
                if attempt < attempts:
                    continue
                return VerificationResult(
                    passed=False,
                    environment=deployment.environment,
                    sha=deployment.sha,
                    url=deployment.url,
                    command=command,
                    output="\n\n".join(outputs)[-100_000:],
                    reason=(
                        "Playwright verification timed out"
                        if attempts == 1
                        else f"Playwright verification timed out after {attempts} attempts"
                    ),
                )
            rendered = output.decode(errors="replace")
            outputs.append(f"Attempt {attempt}/{attempts}:\n{rendered}")
            if process.returncode == 0:
                return VerificationResult(
                    passed=True,
                    environment=deployment.environment,
                    sha=deployment.sha,
                    url=deployment.url,
                    command=command,
                    output="\n\n".join(outputs)[-100_000:],
                )
        return VerificationResult(
            passed=False,
            environment=deployment.environment,
            sha=deployment.sha,
            url=deployment.url,
            command=command,
            output="\n\n".join(outputs)[-100_000:],
            reason=(
                "Playwright command failed"
                if attempts == 1
                else f"Playwright command failed after {attempts} attempts"
            ),
        )
