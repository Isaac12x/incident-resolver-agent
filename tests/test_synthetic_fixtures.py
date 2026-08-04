from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import ConnectorConfig
from src.connectors import ConnectorManager
from src.models import Incident

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("filename", "source", "evidence_kind"),
    [
        ("incident_sentry_failure.json", "sentry", "sentry_issue"),
        ("incident_loki_failure.json", "loki", "loki_log"),
        ("incident_sentry_loki_failure.json", "sentry", "sentry_issue"),
    ],
)
def test_each_normalized_incident_type_is_valid(
    filename: str, source: str, evidence_kind: str
) -> None:
    incident = Incident.model_validate_json((FIXTURES / filename).read_text())
    assert incident.source == source
    assert incident.repository == "company/application"
    assert incident.environment == "production"
    assert incident.evidence[0].kind == evidence_kind
    assert incident.received_at.tzinfo is not None


def test_sentry_webhook_and_event_fixture_have_failure_context() -> None:
    webhook = json.loads((FIXTURES / "sentry_issue_webhook.json").read_text())
    issue = webhook["data"]["issue"]
    event = webhook["data"]["event"]
    assert webhook["action"] == "created"
    assert issue["environment"] == "production"
    assert issue["shortId"] == "CHECKOUT-1842"
    assert event["exception"]["values"][0]["type"] == "AttributeError"
    assert event["request"]["headers"]["x-request-id"] == "req-synthetic-7f3a"

    standalone = json.loads((FIXTURES / "sentry_event_failure.json").read_text())
    assert standalone["level"] == "error"
    assert standalone["transaction"] == "POST /v1/checkout"
    assert standalone["contexts"]["trace"]["trace_id"] == "trace-synthetic-1842"


def test_loki_query_and_response_fixture_have_correlated_failure_lines() -> None:
    query = json.loads((FIXTURES / "loki_failure_query.json").read_text())
    response = json.loads((FIXTURES / "loki_failure_response.json").read_text())
    assert query["query"].startswith('{app="checkout-api"')
    stream = response["data"]["result"][0]
    assert stream["stream"]["env"] == "production"
    assert len(stream["values"]) == 3
    error_line = json.loads(stream["values"][0][1])
    assert error_line["status"] == 500
    assert error_line["trace_id"] == "trace-synthetic-1842"


def test_fixture_incidents_normalize_through_connector_manager() -> None:
    manager = ConnectorManager(
        [
            ConnectorConfig(name="sentry", type="webhook"),
            ConnectorConfig(name="loki", type="webhook"),
        ]
    )
    for filename, connector in (
        ("incident_sentry_failure.json", "sentry"),
        ("incident_loki_failure.json", "loki"),
    ):
        payload = json.loads((FIXTURES / filename).read_text())
        normalized = manager.normalize_incident(connector, payload)
        assert normalized.external_id == payload["external_id"]
        assert normalized.source == connector
