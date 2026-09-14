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
from .storage import Storage
from .tooling import build_repository_graphs
from .verify import DeploymentVerifier
from .workflow import WorkflowEngine


@dataclass
class Application:
    config: Config
    storage: Storage
    connectors: ConnectorManager
    github: GitHubService
    agent: IncidentAgent
    verifier: DeploymentVerifier
    workflow: WorkflowEngine

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
        )
        github = GitHubService(
            config.github,
            webhook_secret=os.getenv(config.github.webhook_secret_env),
            api=GitHubCLIAdapter(config, storage)
            if any(
                repository.publish_mode == "github" or "github.com" in (repository.clone_url or "")
                for repository in config.repositories
            )
            else None,
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
        return cls(config, storage, connectors, github, agent, verifier, workflow)
