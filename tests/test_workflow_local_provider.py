from types import SimpleNamespace

from src.workflow import WorkflowEngine


def test_local_mode_disables_provider_even_if_provider_would_publish() -> None:
    engine = object.__new__(WorkflowEngine)
    engine.config = SimpleNamespace(
        repository=lambda name: SimpleNamespace(publish_mode="local")
    )
    engine.source_control_providers = {
        "github": SimpleNamespace(
            publishing_enabled=lambda mode: True,
            publish_enabled=True,
        )
    }

    assert engine.provider_enabled_for_repository("repo") is False


def test_event_repository_validation_preserves_configured_case() -> None:
    result = WorkflowEngine._event_repositories(
        {
            "repository": {"full_name": "Org/Repo"},
            "deployment": {"repository": {"full_name": "org/repo"}},
        },
        ("ORG/REPO", 7),
    )

    assert result == ({"org/repo"}, "Org/Repo")
