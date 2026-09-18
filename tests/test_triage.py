from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from src.agent import IncidentAgent
from src.app import Application
from src.config import Config, RepositoryConfig, TriageConfig, load_config, save_config
from src.connectors import ConnectorManager
from src.github import GitHubService
from src.models import Incident, TaskState
from src.server import create_server
from src.storage import RepositoryBusyError, Storage
from src.systemd_env import referenced_env_vars
from src.triage import assess_incident, bounded_state, questions, validate_answers
from src.verify import DeploymentVerifier
from src.workflow import WorkflowEngine


def incident():
    return Incident(
        external_id="triage",
        source="grafana",
        repository="org/repo",
        environment="production",
        summary="Database unavailable",
    )


def response(category="infrastructure", sufficient=0.99, code_fix=0.01):
    asked = questions([])
    return {
        "model": "jev-1.13.0",
        "answers": {
            "category": {
                "type": "choice",
                "choice": category,
                "confidence": 0.99,
                "probabilities": {
                    key: float(key == category) for key in asked["category"]["criteria"]
                },
            },
            "impact": {
                "type": "score",
                "score": 2.0,
                "confidence": 0.9,
                "probabilities": {"0": 0.0, "1": 0.0, "2": 1.0, "3": 0.0},
                "legend": {str(i): label for i, label in enumerate(asked["impact"]["criteria"])},
            },
            "evidence_sufficient": {"type": "noul", "noul": sufficient},
            "code_fix": {"type": "noul", "noul": code_fix},
            "correlation": {
                "type": "choice",
                "choice": "none",
                "confidence": 0.99,
                "probabilities": {"none": 1.0},
            },
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "category", "sufficient", "code_fix", "recommendation", "route"),
    [
        ("shadow", "infrastructure", 0.99, 0.01, "operator_review", "agent"),
        ("enforce", "infrastructure", 0.99, 0.01, "operator_review", "operator_review"),
        ("enforce", "application", 0.99, 0.99, "investigate", "agent"),
        ("enforce", "unknown", 0.3, 0.5, "gather_evidence", "agent"),
        ("enforce", "configuration", 0.99, 0.8, "investigate", "agent"),
    ],
)
async def test_policy(monkeypatch, mode, category, sufficient, code_fix, recommendation, route):
    monkeypatch.setenv("TYPESAFE_API_KEY", "secret")

    def handle(request):
        assert request.headers["Authorization"] == "Bearer secret"
        data = json.loads(request.content)
        assert len(data["questions"]) == 4
        assert "correlation" not in data["questions"]
        assert data["state"]["incident"]["summary"] == incident().summary
        return httpx.Response(200, json=response(category, sufficient, code_fix))

    result = await assess_incident(
        TriageConfig(enabled=True, mode=mode), incident(), [], transport=httpx.MockTransport(handle)
    )
    assert result["status"] == "assessed"
    assert result["recommendation"] == recommendation
    assert result["route"] == route
    assert result["model"] == "jev-1.13.0"
    assert result["request_sha256"] and result["question_version"]
    assert "secret" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [401, 429, 529, 500, "oversize", "json", "schema", "timeout"])
async def test_failure_falls_back_without_exposing_provider_body(monkeypatch, failure):
    monkeypatch.setenv("TYPESAFE_API_KEY", "secret")
    calls = 0

    async def handle(request):
        nonlocal calls
        calls += 1
        if failure == "timeout":
            await asyncio.sleep(0.1)
        if failure == "oversize":
            return httpx.Response(200, content=b"secret" * 12000)
        if failure == "json":
            return httpx.Response(200, text="secret")
        if failure == "schema":
            return httpx.Response(200, json={"model": "secret", "answers": {}})
        return httpx.Response(failure if isinstance(failure, int) else 200, text="secret")

    result = await assess_incident(
        TriageConfig(
            enabled=True, mode="enforce", timeout_seconds=0.02 if failure == "timeout" else 3
        ),
        incident(),
        [],
        transport=httpx.MockTransport(handle),
    )
    assert result["status"] == "fallback" and result["route"] == "agent"
    assert "secret" not in json.dumps(result)
    assert calls == (3 if failure in (429, 529) else 1)


@pytest.mark.asyncio
async def test_retry_then_success_and_disabled_or_missing_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert (await assess_incident(TriageConfig(), incident(), []))["reason"] == "disabled"
    config = TriageConfig(enabled=True)
    assert (await assess_incident(config, incident(), []))["reason"] == "missing_api_key"
    monkeypatch.setenv("TYPESAFE_API_KEY", "secret")
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(529) if len(calls) == 1 else httpx.Response(200, json=response())

    assert (await assess_incident(config, incident(), [], transport=httpx.MockTransport(handle)))[
        "status"
    ] == "assessed"


@pytest.mark.parametrize(
    "mutation", ["root", "model", "missing", "nan", "sum", "keys", "choice", "winner", "legend"]
)
def test_invalid_answers_rejected(mutation):
    payload = response()
    if mutation == "root":
        payload = []
    elif mutation == "model":
        payload["model"] = None
    elif mutation == "missing":
        del payload["answers"]["impact"]
    elif mutation == "nan":
        payload["answers"]["code_fix"]["noul"] = float("nan")
    elif mutation == "sum":
        payload["answers"]["category"]["probabilities"]["unknown"] = 0.2
    elif mutation == "keys":
        del payload["answers"]["category"]["probabilities"]["unknown"]
    elif mutation == "choice":
        payload["answers"]["category"]["choice"] = "invented"
    elif mutation == "winner":
        payload["answers"]["category"]["choice"] = "unknown"
    else:
        payload["answers"]["impact"]["legend"] = {}
    with pytest.raises(ValueError):
        validate_answers(payload, questions([]))


def make_application(tmp_path, mode="enforce"):
    config = Config(
        runtime_root=tmp_path / ".agent",
        triage=TriageConfig(enabled=True, mode=mode),
        repositories=[RepositoryConfig(name="org/repo")],
    )
    storage = Storage(config.runtime_root)
    connectors = ConnectorManager([])
    agent = IncidentAgent(config, storage, connectors)
    github = GitHubService(config.github)
    verifier = DeploymentVerifier(config)
    workflow = WorkflowEngine(config, storage, agent, github, verifier)
    return Application(config, storage, connectors, github, agent, verifier, workflow)


@pytest.mark.asyncio
async def test_hold_restart_release_and_assessment_reuse(tmp_path, monkeypatch):
    app = make_application(tmp_path)
    task = await app.workflow.submit(incident())
    monkeypatch.setattr(
        app.workflow, "_worktree", AsyncMock(side_effect=AssertionError("too early"))
    )
    assert (await app.workflow.process(task.task_id)).state == TaskState.TRIAGING
    assert app.storage.load_task(task.task_id).triage is None
    assess = AsyncMock(
        return_value={
            "route": "operator_review",
            "recommendation": "operator_review",
            "mode": "enforce",
            "status": "assessed",
        }
    )
    monkeypatch.setattr("src.workflow.assess_incident", assess)
    assert (await app.workflow.process(task.task_id)).state == TaskState.BLOCKED
    assert (await app.workflow.process(task.task_id)).state == TaskState.BLOCKED
    assert (app.storage.task_directory(task.task_id) / "artifacts/triage.json").exists()
    app = make_application(tmp_path)  # Reload SQLite, not the filesystem assessment.
    await app.workflow.recover()
    assert app.workflow._wakeups.empty()
    with pytest.raises(ValueError, match="busy"):
        app.storage.catalog.acquire(task.task_id, "other", 60)
        await app.workflow.release_triage(task.task_id)
    app.storage.catalog.release(task.task_id, "other")
    released = await app.workflow.release_triage(task.task_id)
    assert released.triage["operator_released"]
    events = app.storage.events(task.task_id)
    assert any(event.type == "triage.assessed" for event in events)
    assert events[-1].type == "triage.released"
    worktree = AsyncMock(return_value=tmp_path)
    monkeypatch.setattr(app.workflow, "_worktree", worktree)
    assert (await app.workflow.process(task.task_id)).state == TaskState.COLLECTING_CONTEXT
    assess.assert_awaited_once()
    assert (
        "operator_released" in (app.storage.task_directory(task.task_id) / "context.md").read_text()
    )
    with pytest.raises(ValueError, match="not awaiting"):
        await app.workflow.release_triage(task.task_id)


@pytest.mark.asyncio
async def test_shadow_and_recovery_before_worktree_with_scoped_candidates(tmp_path, monkeypatch):
    app = make_application(tmp_path, "shadow")
    task = await app.workflow.submit(incident())
    await app.workflow.process(task.task_id)
    await app.workflow.recover()
    assert task.task_id in app.workflow._queued_task_ids
    records = [
        {"task_id": key, "repository": repo, "environment": env}
        for key, repo, env in [
            (task.task_id, "org/repo", "production"),
            ("other-repo", "other/repo", "production"),
            ("staging", "org/repo", "staging"),
            ("match", "ORG/repo", "production"),
        ]
    ]
    monkeypatch.setattr(
        app.workflow, "similar_incidents_search", lambda *a, **kw: {"results": records}
    )
    assess = AsyncMock(return_value={"route": "agent", "recommendation": "operator_review"})
    monkeypatch.setattr("src.workflow.assess_incident", assess)
    monkeypatch.setattr(app.workflow, "_worktree", AsyncMock(side_effect=RepositoryBusyError()))
    await app.workflow.process(task.task_id)
    assert assess.call_args.args[2] == [records[-1]]
    assert app.storage.load_task(task.task_id).triage["route"] == "agent"
    monkeypatch.setattr(app.workflow, "_worktree", AsyncMock(return_value=tmp_path))
    assert (await app.workflow.process(task.task_id)).state == TaskState.COLLECTING_CONTEXT
    assess.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_during_triage_does_not_dispatch(tmp_path, monkeypatch):
    app = make_application(tmp_path)
    task = await app.workflow.submit(incident())
    await app.workflow.process(task.task_id)

    async def assess(*args):
        app.workflow.cancel(task.task_id)
        return {"route": "agent"}

    monkeypatch.setattr("src.workflow.assess_incident", assess)
    monkeypatch.setattr(app.workflow, "_worktree", AsyncMock(side_effect=AssertionError("called")))
    assert (await app.workflow.process(task.task_id)).state == TaskState.CANCELLED


def test_config_bounds_and_secret_reference(tmp_path):
    config = Config(triage=TriageConfig(enabled=True, mode="enforce", api_key_env="TRIAGE_KEY"))
    path = tmp_path / "config.toml"
    save_config(config, path)
    assert load_config(path).triage == config.triage
    assert "TRIAGE_KEY" in referenced_env_vars(config)
    for values in [{"review_threshold": 0.5}, {"timeout_seconds": 0}, {"api_key_env": "bad key"}]:
        with pytest.raises(ValueError):
            TriageConfig(**values)
    sample = incident().model_copy(update={"description": "x" * 100000})
    state = bounded_state(sample, [{"task_id": "one", "summary": "x"}] * 20)
    assert len(state["incident"]["description"]) == 8000
    assert len(state["candidates"]) == 5


def test_release_api_auth_and_errors(tmp_path, monkeypatch):
    app = make_application(tmp_path)
    monkeypatch.setenv(app.config.server.api_token_env, "token")
    task = app.storage.create_task(incident())
    app.storage.transition(task.task_id, TaskState.BLOCKED, triage={"route": "operator_review"})
    path = f"/mcp/tools/release_triage/{task.task_id}"
    with TestClient(create_server(app, run_worker=False)) as client:
        assert client.post(path).status_code == 401
        headers = {"Authorization": "Bearer token"}
        assert client.post(path, headers=headers).status_code == 200
        assert client.post(path, headers=headers).status_code == 409
        assert client.post("/mcp/tools/release_triage/missing", headers=headers).status_code == 404


@pytest.mark.asyncio
async def test_tui_triage_configuration(tmp_path):
    from textual.widgets import Checkbox, Input, Select

    from src.tui import ConfigurationApp

    path = tmp_path / "config.toml"
    save_config(Config(), path)
    app = ConfigurationApp(path)
    async with app.run_test():
        app.query_one("#triage-enabled", Checkbox).value = True
        app.query_one("#triage-mode", Select).value = "enforce"
        app.query_one("#triage-key-env", Input).value = "TRIAGE_SECRET"
        app.query_one("#triage-threshold", Input).value = "0.98"
        draft = app._collect()
        save_config(draft, path)
    loaded = load_config(path)
    assert loaded.triage.enabled and loaded.triage.mode == "enforce"
    assert loaded.triage.review_threshold == 0.98
    assert loaded.triage.api_key_env == "TRIAGE_SECRET"


@pytest.mark.asyncio
async def test_candidate_correlation_is_advisory(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "secret")
    candidates = [{"task_id": "existing", "summary": "Database unavailable"}]

    def handle(request):
        data = json.loads(request.content)
        assert set(data["questions"]["correlation"]["criteria"]) == {"none", "existing"}
        result = response("application", code_fix=0.99)
        result["answers"]["correlation"].update(
            choice="existing", probabilities={"none": 0.0, "existing": 1.0}
        )
        return httpx.Response(200, json=result)

    result = await assess_incident(
        TriageConfig(enabled=True, mode="enforce"),
        incident(),
        candidates,
        transport=httpx.MockTransport(handle),
    )
    assert result["status"] == "assessed"
    assert result["answers"]["correlation"]["choice"] == "existing"
    assert result["route"] == "agent"
