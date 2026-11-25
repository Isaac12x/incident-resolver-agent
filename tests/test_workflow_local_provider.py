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
    local_auto = SimpleNamespace(publish_mode="auto", github_health_checkable=False)
    explicit_github = SimpleNamespace(github_health_checkable=True)
    engine.config = SimpleNamespace(
        repository=lambda name: local_auto,
        repositories=[local_auto, explicit_github],
    )
    provider = SimpleNamespace(publish_enabled=True)
    engine.source_control_providers = {"github": provider}

    assert engine.provider_enabled_for_repository("auto-local") is True


def test_plugin_selected_github_clone_does_not_enable_unrelated_auto_repository():
    engine = object.__new__(WorkflowEngine)
    auto_repository = SimpleNamespace(publish_mode="auto", github_health_checkable=False)
    plugin_repository = SimpleNamespace(github_health_checkable=False)
    engine.config = SimpleNamespace(
        repository=lambda name: auto_repository,
        repositories=[auto_repository, plugin_repository],
    )
    provider = SimpleNamespace(publish_enabled=True)
    engine.source_control_providers = {"github": provider}

    assert engine.provider_enabled_for_repository("auto-local") is False


def test_event_repository_validation_preserves_configured_case() -> None:
    result = WorkflowEngine._event_repositories(
        {
            "repository": {"full_name": "Org/Repo"},
            "deployment": {"repository": {"full_name": "org/repo"}},
        },
        ("ORG/REPO", 7),
    )

    assert result == ({"org/repo"}, "Org/Repo")
