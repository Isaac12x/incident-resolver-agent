"""Application composition root."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .agent import AgentBackend, IncidentAgent, OpenAIAgentsBackend, SubscriptionCLIBackend
from .bundles import load_active_bundle
from .config import Config, load_config
from .connectors import ConnectorManager
from .github import GitHubCLIAdapter, GitHubService
from .source_control import (
    SourceControlProvider,
    create_source_control_provider,
    register_source_control_provider,
)
from .storage import Storage
from .tooling import build_repository_graphs
from .verify import DeploymentVerifier
from .workflow import WorkflowEngine


def _github_provider(config: Config, storage: Storage) -> GitHubService:
    return GitHubService(
        config.github,
        webhook_secret=os.getenv(config.github.webhook_secret_env),
        api=GitHubCLIAdapter(config, storage)
        if any(
            repository.publish_mode == "github"
            or (
                repository.publish_mode in {"auto", "local"}
                and "github.com" in (repository.clone_url or "")
            )
            for repository in config.repositories
        )
        else None,
    )


register_source_control_provider("github", _github_provider)


@dataclass
class Application:
    config: Config
    storage: Storage
    connectors: ConnectorManager
    github: GitHubService
    agent: IncidentAgent
    verifier: DeploymentVerifier
    workflow: WorkflowEngine
    source_control_providers: dict[str, SourceControlProvider] | None = None

    @property
    def source_control(self):
        """Registered providers keyed by their ``publish_mode`` values."""
        return self.source_control_providers or {"github": self.github}

    @classmethod
    def build(
        cls,
        config_path: Path | str = ".agent/config.toml",
        *,
        agent_backend: AgentBackend | None = None,
    ) -> Application:
        load_dotenv(Path(config_path).resolve().parent / ".env", override=False)
        load_dotenv(Path.cwd() / ".env", override=False)
        config = load_config(Path(config_path))
        bundle = load_active_bundle(config)
        if bundle is not None:
            effective = Config.model_validate(json.loads(bundle.config_path.read_text()))
            effective.runtime_root = config.runtime_root
            config = effective
        storage = Storage(config.runtime_root)
        connectors = ConnectorManager(
            config.connectors,
            default_repository=(
                config.repositories[0].name if len(config.repositories) == 1 else None
            ),
            default_application=(
                config.applications[0].name if len(config.applications) == 1 else None
            ),
        )
        provider_names = {
            repository.publish_mode.strip().casefold()
            for repository in config.repositories
            if repository.publish_mode.strip().casefold() not in {"auto", "local", "github"}
        }
        github = _github_provider(config, storage)
        providers: dict[str, SourceControlProvider] = {"github": github}
        for provider_name in provider_names:
            providers[provider_name] = create_source_control_provider(
                provider_name, config, storage
            )
        backend = agent_backend or (
            SubscriptionCLIBackend(config)
            if config.model.runtime == "subscription-cli"
            else OpenAIAgentsBackend(config)
        )
        agent = IncidentAgent(
            config,
            storage,
            connectors,
            backend,
            **({"skills_root": bundle.path / "skills"} if bundle else {}),
        )
        verifier = DeploymentVerifier(config)
        workflow = WorkflowEngine(
            config,
            storage,
            agent,
            github,
            verifier,
            source_control_providers=providers,
            repository_indexer=build_repository_graphs,
        )

        def reload_model() -> None:
            # Read the policy as data on every poll so package upgrades are visible
            # even to an already imported worker. In-flight calls finish normally.
            path = Path(config_path)
            if not path.is_file() or not path.stat().st_size:
                raise ValueError("model configuration is missing or empty")
            latest = load_config(path)
            active = load_active_bundle(latest)
            updated = (
                Config.model_validate(json.loads(active.config_path.read_text())).model
                if active
                else latest.model
            )
            if updated == config.model:
                return
            config.model = updated
            if agent_backend is None:
                agent.backend = (
                    SubscriptionCLIBackend(config)
                    if updated.runtime == "subscription-cli"
                    else OpenAIAgentsBackend(config)
                )
            logging.getLogger(__name__).warning(
                "Model settings reloaded: %s/%s (%s)",
                updated.name,
                updated.reasoning,
                updated.runtime,
            )

        workflow.reload_model = reload_model
        return cls(config, storage, connectors, github, agent, verifier, workflow, providers)
