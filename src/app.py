"""Application composition root."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .agent import AgentBackend, IncidentAgent, OpenAIAgentsBackend
from .config import Config, load_config
from .connectors import ConnectorManager
from .github import GitHubService
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
        load_dotenv(Path.cwd() / ".env", override=False)
        config = load_config(Path(config_path))
        storage = Storage(config.runtime_root)
        connectors = ConnectorManager(config.connectors)
        github = GitHubService(
            config.github, webhook_secret=os.getenv(config.github.webhook_secret_env)
        )
        backend = agent_backend or OpenAIAgentsBackend(config)
        agent = IncidentAgent(config, storage, connectors, backend)
        verifier = DeploymentVerifier(config)
        workflow = WorkflowEngine(
            config,
            storage,
            agent,
            github,
            verifier,
            repository_indexer=build_repository_graphs,
        )
        return cls(config, storage, connectors, github, agent, verifier, workflow)
