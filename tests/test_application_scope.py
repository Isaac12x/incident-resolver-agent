from __future__ import annotations

from pathlib import Path

import pytest

from src.config import (
    ApplicationConfig,
    Config,
    ConnectorConfig,
    RepositoryConfig,
    load_config,
    save_config,
)
from src.connectors import ConnectorManager


def _config() -> Config:
    return Config(
        repositories=[RepositoryConfig(name="org/api"), RepositoryConfig(name="org/web")],
        applications=[
            ApplicationConfig(
                name="storefront",
                services=["api", "web"],
                repositories=["org/api", "org/web"],
                integration_command="python integration.py",
            ),
            ApplicationConfig(name="admin", services=["admin"], repositories=["org/api"]),
        ],
    )


def test_application_config_round_trip_and_membership_validation(tmp_path: Path) -> None:
    config = _config()
    path = tmp_path / "config.toml"
    save_config(config, path)
    loaded = load_config(path, create=False)
    assert loaded.application("STOREFRONT").integration_command == "python integration.py"
    assert loaded.application("storefront").repositories == ["org/api", "org/web"]

    with pytest.raises(ValueError, match="multiple applications"):
        config.resolve_application(repository="org/api")
    with pytest.raises(ValueError, match="not a member"):
        config.resolve_application(application="admin", service="web")
    with pytest.raises(ValueError, match="does not match"):
        config.resolve_application(service="missing")
    with pytest.raises(ValueError, match="safe"):
        RepositoryConfig(name="../outside")
    with pytest.raises(ValueError, match="unsafe"):
        Config(repositories=[RepositoryConfig(name="repo.lock")])
    with pytest.raises(ValueError, match="configured more than once"):
        Config(repositories=[RepositoryConfig(name="org/api"), RepositoryConfig(name="ORG/API")])
    with pytest.raises(ValueError, match="configured more than once"):
        Config(
            repositories=[RepositoryConfig(name="org/api")],
            applications=[
                ApplicationConfig(name="app", repositories=["org/api"]),
                ApplicationConfig(name="APP", repositories=["org/api"]),
            ],
        )
    with pytest.raises(ValueError, match="unconfigured"):
        Config(
            repositories=[RepositoryConfig(name="org/api")],
            applications=[ApplicationConfig(name="app", repositories=["org/missing"])],
        )
    with pytest.raises(KeyError):
        config.application("missing")
    with pytest.raises(KeyError):
        config.repository("missing")
    with pytest.raises(ValueError, match="repository .* member"):
        config.resolve_application(application="admin", repository="org/web")
    with pytest.raises(ValueError, match="no application"):
        config.resolve_application()
    assert Config().resolve_application() is None


def test_connector_normalizes_application_and_service_without_repository() -> None:
    manager = ConnectorManager([ConnectorConfig(name="alerts", type="webhook")])
    incident = manager.normalize_incident(
        "alerts",
        {
            "external_id": "alert-1",
            "application": "storefront",
            "service": "web",
            "environment": "production",
            "summary": "frontend is unavailable",
        },
    )
    assert incident.repository == ""
    assert incident.application == "storefront"
    assert incident.service == "web"
