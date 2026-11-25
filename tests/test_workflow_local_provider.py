from types import SimpleNamespace

from src.workflow import WorkflowEngine


def test_local_mode_disables_provider_even_if_provider_would_publish() -> None:
    engine = object.__new__(WorkflowEngine)
    engine.config = SimpleNamespace(
        repository=lambda name: SimpleNamespace(publish_mode="local")
    )
    engine.source_control_providers = {
        "github": SimpleNamespace(
            publish_enabled=True,
        )
    }

    assert engine.provider_enabled_for_repository("repo") is False


def test_auto_mode_keeps_legacy_global_github_selection():
    engine = object.__new__(WorkflowEngine)
    engine.config = SimpleNamespace(repository=lambda name: SimpleNamespace(publish_mode="auto"))
    provider = SimpleNamespace(publish_enabled=True)
    engine.source_control_providers = {"github": provider}

    assert engine.provider_enabled_for_repository("auto-local") is True


def test_explicit_github_mode_keeps_publication_enabled_without_api_adapter():
    engine = object.__new__(WorkflowEngine)
    engine.config = SimpleNamespace(repository=lambda name: SimpleNamespace(publish_mode="github"))
    engine.source_control_providers = {"github": SimpleNamespace(publish_enabled=False)}

    assert engine.provider_enabled_for_repository("repo") is True


def test_plugin_mode_selects_its_registered_provider():
    engine = object.__new__(WorkflowEngine)
    engine.config = SimpleNamespace(
        repository=lambda name: SimpleNamespace(publish_mode="example")
    )
    provider = SimpleNamespace(publish_enabled=False)
    engine.source_control_providers = {"github": object(), "example": provider}

    assert engine.source_control_name_for_repository("plugin-repo") == "example"
    assert engine.provider_for_repository("plugin-repo") is provider


def test_event_repository_validation_preserves_configured_case() -> None:
    result = WorkflowEngine._event_repositories(
        {
            "repository": {"full_name": "Org/Repo"},
            "deployment": {"repository": {"full_name": "org/repo"}},
        },
        ("ORG/REPO", 7),
    )

    assert result == ({"org/repo"}, "Org/Repo")
