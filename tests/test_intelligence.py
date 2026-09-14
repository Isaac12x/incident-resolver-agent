from __future__ import annotations

import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.agent import IncidentAgent
from src.app import Application
from src.config import Config, RepositoryConfig
from src.connectors import ConnectorManager
from src.github import GitHubService
from src.intelligence import (
    LogisticRootCauseModel,
    SimilarIncidentSearch,
    summarize_incident,
)
from src.models import Incident
from src.server import create_server
from src.storage import Storage
from src.verify import DeploymentVerifier
from src.workflow import WorkflowEngine


def test_event_groups_and_deduplicates_payload(tmp_path: Path) -> None:
    storage = Storage(tmp_path / ".agent")
    payload = {"groupKey": "alerts", "alerts": [{"fingerprint": "abc"}]}
    first = storage.record_observability_event("grafana", payload)
    second = storage.record_observability_event("grafana", payload)
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["duplicate_of"] == first["event_id"]
    assert storage.list_observability_events(group_key="alerts")[0]["fingerprint"] == "abc"


def test_event_filters_and_history_update_preserve_labels(tmp_path: Path) -> None:
    storage = Storage(tmp_path / ".agent")
    event = storage.record_observability_event("grafana", {"external_id": "a"})
    storage.attach_event_task(str(event["event_id"]), "task-a")
    assert storage.list_observability_events(source="sentry") == []
    assert storage.list_observability_events()[0]["task_id"] == "task-a"
    incident = Incident(
        external_id="a", source="test", repository="r", environment="production", summary="x"
    )
    storage.record_incident_history("task-a", incident, root_cause="database")
    storage.record_incident_history("task-a", incident)
    assert storage.incident_history(labeled_only=True)[0]["root_cause"] == "database"
    with pytest.raises(ValueError):
        storage.list_observability_events(limit=0)


def test_concurrent_event_ingestion_has_one_canonical_event(tmp_path: Path) -> None:
    storage = Storage(tmp_path / ".agent")
    payload = {"groupKey": "concurrent", "alerts": [{"fingerprint": "same"}]}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _: storage.record_observability_event("grafana", payload), range(8))
        )
    assert sum(not bool(result["duplicate"]) for result in results) == 1
    assert len(storage.list_observability_events()) == 1


def test_incident_scope_and_intelligence_http_api(tmp_path: Path) -> None:
    config = Config(
        runtime_root=tmp_path / ".agent",
        repositories=[RepositoryConfig(name="r", incident_environments=["production", "staging"])],
    )
    storage = Storage(config.runtime_root)
    connectors = ConnectorManager([])
    agent = IncidentAgent(config, storage, connectors)
    github = GitHubService(config.github)
    workflow = WorkflowEngine(config, storage, agent, github, DeploymentVerifier(config))
    application = Application(
        config, storage, connectors, github, agent, DeploymentVerifier(config), workflow
    )
    with TestClient(create_server(application, run_worker=False)) as client:
        submitted = client.post(
            "/mcp/tools/submit_incident",
            json={
                "external_id": "same",
                "source": "grafana",
                "repository": "r",
                "environment": "production",
                "summary": "database timeout",
            },
        )
        assert submitted.status_code == 200
        task_id = submitted.json()["task_id"]
        assert client.get(f"/mcp/resources/tasks/{task_id}/summary").status_code == 200
        assert client.get("/mcp/resources/intelligence/events").status_code == 200
        assert (
            client.post("/mcp/tools/predict_root_cause", json={"text": "timeout"}).status_code
            == 200
        )
        assert (
            client.post("/mcp/tools/search_similar_incidents", json={"text": "timeout"}).status_code
            == 200
        )
        assert client.post("/mcp/tools/rebuild_intelligence").status_code == 200
        assert client.post("/mcp/tools/predict_root_cause", json={}).status_code == 422
        assert (
            client.post(
                "/mcp/tools/search_similar_incidents", json={"text": "x", "limit": None}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/mcp/tools/search_similar_incidents", json={"text": "x", "limit": 21}
            ).status_code
            == 422
        )
        assert client.get("/mcp/resources/tasks/missing/summary").status_code == 404
        assert client.post("/hooks/incidents/grafana", json=[]).status_code == 422
    second = Incident(
        external_id="same", source="grafana", repository="r", environment="staging", summary="x"
    )
    first = storage.load_incident(task_id)
    assert storage.find_by_incident(
        first.source, first.external_id, first.repository, first.environment
    )
    assert (
        storage.find_by_incident(
            second.source, second.external_id, second.repository, second.environment
        )
        is None
    )


def test_history_trains_predictor_and_summary(tmp_path: Path) -> None:
    storage = Storage(tmp_path / ".agent")
    for index, cause in enumerate(
        ("database timeout", "database timeout", "cache outage", "cache outage")
    ):
        incident = Incident(
            external_id=str(index),
            source="test",
            repository="r",
            environment="production",
            summary=cause,
            description="request failed",
        )
        task = storage.create_task(incident)
        storage.record_incident_history(task.task_id, incident, root_cause=cause)
    model = LogisticRootCauseModel.train(storage.incident_history(labeled_only=True))
    prediction = model.predict("database timeout request")
    assert prediction["available"] is True
    assert prediction["predictions"][0]["root_cause"] == "database timeout"
    assert (
        summarize_incident("First sentence. Second sentence.")["summary"]
        == "First sentence. Second sentence."
    )


def test_untrained_model_and_optional_search_are_honest() -> None:
    assert LogisticRootCauseModel.train([]).predict("anything")["available"] is False
    search = SimilarIncidentSearch()
    result = search.search("anything")
    assert result["available"] is False
    assert result["results"] == []


def test_summary_empty_and_ranked_sentences() -> None:
    assert summarize_incident(" ")["sentences"] == 0
    result = summarize_incident("Noise here. Database timeout repeated. Noise there.")
    assert result["sentences"] == 3
    assert "Database timeout" in result["summary"]


def test_single_label_model_is_explicitly_untrained() -> None:
    model = LogisticRootCauseModel.train([{"summary": "x", "root_cause": "one"}])
    assert model.predict("x")["available"] is False


def test_faiss_search_build_and_query_with_optional_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Vectors:
        shape = (2, 2)

    class Index:
        def __init__(self, dimension: int) -> None:
            self.rows = []

        def add(self, vectors: object) -> None:
            self.rows.append(vectors)

        def search(self, vector: object, limit: int) -> tuple[list[list[float]], list[list[int]]]:
            return [[0.91] * limit], [[0] * limit]

    class Model:
        def __init__(self, name: str) -> None:
            assert name == "all-MiniLM-L6-v2"

        def encode(self, values: list[str], normalize_embeddings: bool = False) -> Vectors:
            assert normalize_embeddings is True
            return Vectors()

    monkeypatch.setitem(sys.modules, "faiss", types.SimpleNamespace(IndexFlatIP=Index))
    monkeypatch.setitem(
        sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=Model)
    )
    search = SimilarIncidentSearch()
    built = search.build([{"summary": "database", "root_cause": "db"}])
    assert built["available"] is True
    assert search.search("database")["results"][0]["root_cause"] == "db"


def test_faiss_empty_history_is_unavailable() -> None:
    result = SimilarIncidentSearch().build([])
    assert result["available"] is False
    assert result["reason"]


def test_lexical_retrieval_is_explicit_when_vector_model_unavailable() -> None:
    search = SimilarIncidentSearch()
    built = search.build([{"task_id": "old", "summary": "database timeout"}])
    result = search.search("database timeout")
    assert result["results"][0]["task_id"] == "old"
    if not built["available"]:
        assert result["reason"]


def test_different_incident_scopes_have_isolated_sessions(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "state")
    incident = Incident(
        external_id="1", source="grafana", repository="r", environment="production", summary="error"
    )
    tasks = [
        storage.create_task(incident.model_copy(update=update))
        for update in (
            {},
            {"environment": "staging"},
            {"repository": "other"},
            {"source": "sentry"},
        )
    ]
    assert len({task.task_id for task in tasks}) == 4
    assert len({task.conversation_id for task in tasks}) == 4
    assert len({task.agent_session_id for task in tasks}) == 4
    assert storage.create_task(incident).task_id == tasks[0].task_id
    assert storage.root.stat().st_mode & 0o777 == 0o700


def test_grafana_alert_labels_scope_event_groups(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "state")
    records = []
    for repository, environment in (("a", "prod"), ("b", "prod"), ("a", "staging")):
        payload = {
            "groupKey": "same",
            "alerts": [
                {
                    "fingerprint": "same",
                    "labels": {
                        "repository": repository,
                        "environment": environment,
                    },
                }
            ],
        }
        records.append(storage.record_observability_event("grafana", payload))
    assert len({record["group_key"] for record in records}) == 3
    assert all(not record["duplicate"] for record in records)


def test_search_waits_for_rebuild_to_finish(tmp_path: Path, monkeypatch) -> None:
    config = Config(runtime_root=tmp_path / "state")
    storage = Storage(config.runtime_root)
    connectors = ConnectorManager([])
    workflow = WorkflowEngine(
        config,
        storage,
        IncidentAgent(config, storage, connectors),
        GitHubService(config.github),
        DeploymentVerifier(config),
    )
    rebuilding, release, requested, searched = (threading.Event() for _ in range(4))

    def build(records):
        rebuilding.set()
        assert release.wait(3)
        workflow.similar_incidents._index = object()
        return {"available": True}

    def search(query, *, limit):
        searched.set()
        return {"available": True, "results": []}

    def request_search():
        requested.set()
        return workflow.similar_incidents_search("timeout")

    monkeypatch.setattr(workflow.similar_incidents, "build", build)
    monkeypatch.setattr(workflow.similar_incidents, "search", search)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rebuilding_future = pool.submit(workflow.rebuild_intelligence)
        assert rebuilding.wait(3)
        searching_future = pool.submit(request_search)
        assert requested.wait(3)
        try:
            assert not searched.wait(0.05)
        finally:
            release.set()
        assert rebuilding_future.result(timeout=3)["similar_incidents"]["available"]
        assert searching_future.result(timeout=3)["available"]


@pytest.mark.asyncio
async def test_show_me_summary_uses_persisted_lifecycle_artifacts(tmp_path, monkeypatch):
    from src.models import TaskState
    from src.workflow import _TaskLifecycle

    config = Config(runtime_root=tmp_path / "state")
    storage = Storage(config.runtime_root)
    connectors = ConnectorManager([])
    github = GitHubService(config.github)
    agent = IncidentAgent(config, storage, connectors)
    verifier = DeploymentVerifier(config)
    workflow = WorkflowEngine(config, storage, agent, github, verifier)
    task = storage.create_task(
        Incident(
            external_id="summary",
            source="test",
            repository="r",
            environment="production",
            summary="Checkout fails.",
        )
    )
    assert workflow.intelligence_summary(task.task_id)["method"] == "extractive-fallback"
    storage.transition(task.task_id, TaskState.INVESTIGATING)
    explanation = (
        "Missing session causes checkout failure.\n\n```mermaid\n"
        "flowchart LR\n  Request --> MissingSession\n```"
    )
    await _TaskLifecycle(workflow, task.task_id, tmp_path).mark_investigation_complete(
        explanation, ["Stack trace points to checkout"], "Validate session before checkout"
    )
    summary = workflow.intelligence_summary(task.task_id)
    assert summary["method"] == "agent-artifact"
    assert summary["source"] == "investigation.md"
    assert explanation in summary["summary"]
    assert "Stack trace points to checkout" in summary["summary"]

    # Summary reads persisted evidence, without a new model call or external executable.
    monkeypatch.setenv("EXPLAIN_CODE_COMMAND", "must-not-run")
    workflow.storage = Storage(config.runtime_root)
    application = Application(
        config, workflow.storage, connectors, github, agent, verifier, workflow
    )
    with TestClient(create_server(application, run_worker=False)) as client:
        assert client.get(f"/mcp/resources/tasks/{task.task_id}/summary").json() == summary
    storage.write_artifact(
        task.task_id, "artifacts/local/fix.txt", "Validated session; regression passed."
    )
    assert workflow.intelligence_summary(task.task_id)["source"] == "artifacts/local/fix.txt"
    storage.write_artifact(task.task_id, "artifacts/local/fix.txt", "  ")
    assert workflow.intelligence_summary(task.task_id)["source"] == "investigation.md"
