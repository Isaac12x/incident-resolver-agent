"""Independent acceptance checks for a coordinated Python/React application repair."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.agent import IncidentAgent, OpenAIAgentsBackend
from src.app import Application
from src.config import ApplicationConfig, Config, ConnectorConfig, RepositoryConfig, save_config
from src.connectors import ConnectorManager
from src.github import GitHubService
from src.models import Incident, PullRequestReference, SessionResult, TaskState, VerificationResult
from src.server import create_server
from src.storage import Storage
from src.verify import DeploymentVerifier
from src.workflow import WorkflowEngine


def test_http_intake_routes_services_and_rejects_conflicting_membership(tmp_path):
    config = Config(
        runtime_root=tmp_path / "runtime",
        repositories=[RepositoryConfig(name="shop/api"), RepositoryConfig(name="shop/web")],
        applications=[
            ApplicationConfig(
                name="storefront",
                services=["api", "web"],
                repositories=["shop/api", "shop/web"],
            ),
            ApplicationConfig(name="admin", services=["admin"], repositories=["shop/api"]),
        ],
        connectors=[ConnectorConfig(name="alerts", type="webhook")],
    )
    path = tmp_path / "config.toml"
    save_config(config, path)
    app = Application.build(path)
    client = TestClient(create_server(app, run_worker=False))
    payload = {"external_id": "checkout-1", "environment": "production", "summary": "Broken"}
    first = client.post("/hooks/incidents/alerts", json={**payload, "service": "web"})
    assert first.status_code == 202, first.text
    task = app.storage.load_task(first.json()["task_id"])
    assert task.application == "storefront"
    assert set(task.repositories) == {"shop/api", "shop/web"}
    duplicate = client.post(
        "/hooks/incidents/alerts",
        json={**payload, "application": "storefront", "service": "api"},
    )
    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()["task_id"] == task.task_id
    separate = client.post("/hooks/incidents/alerts", json={**payload, "service": "admin"})
    assert separate.status_code == 202, separate.text
    assert separate.json()["task_id"] != task.task_id
    for scope in (
        {"application": "admin", "service": "web"},
        {"application": "admin", "repository": "shop/web"},
        {"service": "missing", "repository": "shop/web"},
        {"repository": "shop/api"},
    ):
        response = client.post("/hooks/incidents/alerts", json={**payload, **scope})
        assert response.status_code == 422, response.text


def _checkout(path: Path, files: dict[str, str]) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-qb", "main", str(path)], check=True)
    for name, content in files.items():
        (path / name).write_text(content)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Acceptance",
            "-c",
            "user.email=acceptance@example.test",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    return path


@pytest.mark.asyncio
async def test_integration_cache_rejects_dirty_unchanged_dependency(tmp_path):
    api_source = _checkout(tmp_path / "backend", {"api.py": "value = 1\n"})
    web_source = _checkout(
        tmp_path / "frontend",
        {
            "contract.txt": "compatible",
            "check.py": (
                "from pathlib import Path\n"
                "assert Path(__file__).with_name('contract.txt').read_text() == 'compatible'\n"
            ),
        },
    )
    config = Config(
        runtime_root=tmp_path / "runtime",
        repositories=[
            RepositoryConfig(name="shop/api", local_path=api_source, publish_mode="local"),
            RepositoryConfig(name="shop/web", local_path=web_source, publish_mode="local"),
        ],
        applications=[ApplicationConfig(name="shop", repositories=["shop/api", "shop/web"])],
    )
    storage = Storage(config.runtime_root)
    engine = WorkflowEngine(
        config, storage, object(), GitHubService(config.github), DeploymentVerifier(config)
    )
    task = await engine.submit(
        Incident(
            external_id="cache-1",
            source="test",
            application="shop",
            environment="production",
            summary="Integration cache scope",
        )
    )
    root = await engine._worktree(task)
    web = storage.repository_worktree(task, "shop/web")
    config.applications[0].integration_command = shlex.join(
        [
            sys.executable,
            str((web / "check.py").relative_to(root)),
        ]
    )
    assert await engine.application.run_integration(storage.load_task(task.task_id))
    # No new commit or PR head: changed dependency input still invalidates combined evidence.
    (web / "contract.txt").write_text("incompatible")
    assert not await engine.application.run_integration(storage.load_task(task.task_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_second", [False, True])
async def test_partial_publication_restart_and_two_pr_completion(tmp_path, fail_second):
    names = ["shop/api", "shop/web"]
    config = Config(
        runtime_root=tmp_path / "runtime",
        repositories=[
            RepositoryConfig(
                name=name,
                publish_mode="github",
                local_path=_checkout(
                    tmp_path / name.split("/")[1],
                    {
                        "value.txt": "broken",
                        "check.py": (
                            "from pathlib import Path\n"
                            "assert Path('value.txt').read_text() == 'fixed'\n"
                        ),
                    },
                ),
            )
            for name in names
        ],
        applications=[ApplicationConfig(name="shop", repositories=names)],
    )
    storage = Storage(config.runtime_root)
    publications = []
    sessions = []

    class Gateway(GitHubService):
        def __init__(self):
            super().__init__(config.github, api=self.call)
            self.fail_web_once = fail_second

        async def call(self, operation, payload):
            assert operation == "publish_verification"

        async def create_pull_request(self, task):
            publications.append(task.repository)
            if task.repository == "shop/web" and self.fail_web_once:
                self.fail_web_once = False
                raise RuntimeError("GitHub temporarily unavailable")
            sha = storage.commit_worktree(task, "Fix checkout", task.repository)
            return PullRequestReference(
                repository=task.repository,
                number=17,
                branch=task.branch,
                head_sha=sha,
                url=f"https://github.test/{task.repository}/pull/17",
            )

    class Verifier(DeploymentVerifier):
        async def verify(self, task, deployment, worktree):
            return VerificationResult(
                passed=self.accepts(task, deployment),
                environment=deployment.environment,
                sha=deployment.sha,
                url=deployment.url,
            )

    class Backend(OpenAIAgentsBackend):
        async def __call__(
            self, instructions, prompt, tools, connector_tools, output_type=None, run_context=None
        ):
            sessions.append(run_context.session_id)
            await run_context.lifecycle.mark_investigation_complete(
                "Both components reject checkout",
                ["reproduction"],
                "Fix both contracts",
                True,
            )
            for name in names:
                tools.write_file("value.txt", "fixed", repository=name)
                result = await run_context.lifecycle.run_tests(
                    shlex.join([sys.executable, "check.py"]),
                    repository=name,
                )
                assert result["passed"]
            await run_context.lifecycle.open_pr("Fix both checkout components")
            return {"summary": "Published", "waiting_for_external_event": True}

    agent = IncidentAgent(config, storage, ConnectorManager([]), Backend(config))
    github = Gateway()
    engine = WorkflowEngine(config, storage, agent, github, Verifier(config))
    task = await engine.submit(
        Incident(
            external_id="partial-1",
            source="test",
            application="shop",
            environment="production",
            summary="Checkout broken in both components",
        )
    )
    await engine.process(task.task_id)
    interrupted = await engine.process(task.task_id)
    if fail_second:
        assert interrupted.state == TaskState.PUBLISHING_PR, interrupted.error
        assert interrupted.repositories["shop/api"].pr_number == 17
        assert interrupted.repositories["shop/web"].pr_number is None
    else:
        assert interrupted.state == TaskState.WAITING_FOR_DEPLOYMENT, interrupted.error
    # Restart the workflow with a fresh durable store, retaining only the fake external API.
    engine = WorkflowEngine(config, Storage(config.runtime_root), agent, github, Verifier(config))
    task = await engine.process(task.task_id)
    assert task.state == TaskState.WAITING_FOR_DEPLOYMENT, task.error
    assert publications == (["shop/api", "shop/web", "shop/web"] if fail_second else names)
    assert len(sessions) == 1

    def deployment(name, sha):
        return {
            "repository": {"full_name": name},
            "deployment": {"environment": "preview", "sha": sha, "id": 4},
            "deployment_status": {"state": "success", "environment_url": "https://preview.test"},
        }

    assert (
        await engine.handle_github_event(
            "deployment_status",
            deployment("shop/api", "stale-sha"),
        )
        is None
    )
    for name in names:
        state = task.repositories[name]
        await engine.handle_github_event("deployment_status", deployment(name, state.pr_head_sha))
        task = await engine.process(task.task_id)
        assert task.error is None, task.error
        assert task.state in {TaskState.WAITING_FOR_DEPLOYMENT, TaskState.WAITING_FOR_REVIEW}
    assert task.state == TaskState.WAITING_FOR_REVIEW
    for index, name in enumerate(names):
        task = await engine.handle_github_event(
            "pull_request",
            {
                "action": "closed",
                "repository": {"full_name": name},
                "pull_request": {"number": 17, "merged": True},
            },
        )
        assert task.state == (TaskState.COMPLETED if index == 1 else TaskState.WAITING_FOR_REVIEW)


@pytest.mark.asyncio
async def test_one_session_repairs_python_to_frontend_flow_and_commits_both(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("cross-language acceptance requires Node.js")
    backend_source = _checkout(
        tmp_path / "backend",
        {
            "AGENTS.md": "Backend rule: API monetary values are integer cents.\n",
            "api.py": 'import json\nprint(json.dumps({"total": 1200}))\n',
            "test_api.py": (
                "import json, subprocess, sys\n"
                "data = json.loads(subprocess.check_output([sys.executable, 'api.py']))\n"
                "assert data == {'total_cents': 1200}, data\n"
            ),
            "integration.py": (
                "import json, pathlib, subprocess, sys\n"
                "api = pathlib.Path(__file__).parent / 'api.py'\n"
                "data = subprocess.check_output([sys.executable, str(api)], text=True)\n"
                "result = subprocess.check_output([sys.argv[1], sys.argv[2], data], text=True)\n"
                "assert result.strip() == '12.00', result\n"
            ),
        },
    )
    frontend_source = _checkout(
        tmp_path / "frontend",
        {
            "AGENTS.md": "Frontend rule: display prices with two decimal places.\n",
            "checkout.mjs": (
                "export function formatTotal(data) { return String(data.total); }\n"
                "if (process.argv[2]) console.log(formatTotal(JSON.parse(process.argv[2])));\n"
            ),
            "Checkout.jsx": (
                "import { formatTotal } from './checkout.mjs';\n"
                "export default function Checkout({data}) {\n"
                "  return <output>{formatTotal(data)}</output>;\n}\n"
            ),
            "test_checkout.mjs": (
                "import assert from 'node:assert/strict';\n"
                "import { formatTotal } from './checkout.mjs';\n"
                "assert.equal(formatTotal({total_cents:1200}), '12.00');\n"
            ),
        },
    )
    before = subprocess.run(
        [
            sys.executable,
            str(backend_source / "integration.py"),
            node,
            str(frontend_source / "checkout.mjs"),
        ],
        capture_output=True,
        text=True,
    )
    assert before.returncode != 0 and "1200" in before.stderr

    config = Config(
        runtime_root=tmp_path / "runtime",
        repositories=[
            RepositoryConfig(name="shop/api", local_path=backend_source, publish_mode="local"),
            RepositoryConfig(name="shop/web", local_path=frontend_source, publish_mode="local"),
        ],
        applications=[
            ApplicationConfig(
                name="storefront",
                services=["checkout-api", "checkout-web"],
                repositories=["shop/api", "shop/web"],
            )
        ],
    )
    storage = Storage(config.runtime_root)
    sessions = []

    class Backend(OpenAIAgentsBackend):
        async def __call__(
            self, instructions, prompt, tools, connector_tools, output_type=None, run_context=None
        ):
            assert output_type is SessionResult and run_context is not None
            assert "API monetary values" in instructions
            assert "two decimal places" in instructions
            sessions.append(run_context.session_id)
            await run_context.lifecycle.mark_investigation_complete(
                "API field and frontend money formatting disagree",
                ["The original integrated checkout displays 1200 instead of 12.00"],
                "Return total_cents and render cents as currency units",
                True,
            )
            tools.write_file(
                "api.py",
                'import json\nprint(json.dumps({"total_cents": 1200}))\n',
                repository="shop/api",
            )
            tools.replace_in_file(
                "checkout.mjs",
                "String(data.total)",
                "(data.total_cents / 100).toFixed(2)",
                repository="shop/web",
            )
            for repository, command in [
                ("shop/api", f"{shlex.quote(sys.executable)} test_api.py"),
                ("shop/web", f"{shlex.quote(node)} test_checkout.mjs"),
            ]:
                result = await run_context.lifecycle.run_tests(command, repository=repository)
                assert result["passed"], result
            published = await run_context.lifecycle.open_pr(
                "Align the API money contract and frontend currency rendering."
            )
            assert published["state"] == TaskState.COMPLETED, published
            return {"summary": "Verified the coordinated application repair"}

    agent = IncidentAgent(config, storage, ConnectorManager([]), Backend(config))
    engine = WorkflowEngine(
        config, storage, agent, GitHubService(config.github), DeploymentVerifier(config)
    )
    task = await engine.submit(
        Incident(
            external_id="CHECKOUT-12",
            source="test",
            service="checkout-web",
            environment="production",
            summary="Checkout displays cents as currency units",
        )
    )
    assert task.application == "storefront"
    assert set(task.repositories) == {"shop/api", "shop/web"}
    task = await engine.process(task.task_id)
    assert task.state == TaskState.COLLECTING_CONTEXT, task.error
    root = storage.root / "worktrees" / task.task_id
    api = storage.repository_worktree(task, "shop/api")
    web = storage.repository_worktree(task, "shop/web")
    config.applications[0].integration_command = shlex.join(
        [
            sys.executable,
            str((api / "integration.py").relative_to(root)),
            "node",
            str((web / "checkout.mjs").relative_to(root)),
        ]
    )
    task = await engine.process(task.task_id)
    assert task.state == TaskState.COMPLETED, task.error
    assert sessions == [task.agent_session_id]
    fresh = Storage(config.runtime_root).load_task(task.task_id)
    assert set(fresh.repositories) == {"shop/api", "shop/web"}
    for state in fresh.repositories.values():
        assert state.pr_head_sha and state.pr_url.startswith("local://")
    integration = json.loads(
        (storage.task_directory(task.task_id) / "artifacts/integration.json").read_text()
    )
    assert integration["passed"] is True
    assert json.loads(subprocess.check_output([sys.executable, str(api / "api.py")])) == {
        "total_cents": 1200,
    }
    # The agent worked in isolated branches; the original source checkouts are untouched.
    assert '"total": 1200' in (backend_source / "api.py").read_text()
    assert "String(data.total)" in (frontend_source / "checkout.mjs").read_text()
