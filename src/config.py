"""Configuration loading, validation and secret-safe persistence."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class ModelConfig(BaseModel):
    mode: Literal["local", "remote"] = "remote"
    provider: str = "openai"
    base_url: str | None = None
    api_key_env: str = "OPENAI_API_KEY"
    organization_env: str | None = None
    name: str = "gpt-5"
    reasoning: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = "high"
    temperature: float | None = Field(None, ge=0, le=2)
    top_p: float | None = Field(None, gt=0, le=1)
    max_tokens: int | None = Field(None, ge=1)
    parallel_tool_calls: bool = False
    max_turns_per_iteration: int = Field(30, ge=1)
    max_task_iterations: int = Field(8, ge=1)
    tool_timeout_seconds: int = Field(600, ge=1)

    @model_validator(mode="after")
    def local_mode_has_endpoint(self) -> ModelConfig:
        if self.mode == "local" and not self.base_url:
            raise ValueError("local model mode requires an OpenAI-compatible base_url")
        return self


class TriggerConfig(BaseModel):
    mode: Literal["hook", "workflow", "agent-call"] = "hook"
    hook_path: str = "/hooks/incidents"
    workflow_name: str = ""
    agent_name: str = "incident-agent"
    require_ack: bool = False

    @field_validator("hook_path")
    @classmethod
    def valid_hook_path(cls, value: str) -> str:
        value = value.rstrip("/")
        if (
            not value.startswith("/")
            or value == ""
            or any(character in value for character in "{}?#")
        ):
            raise ValueError("hook_path must be an absolute route without parameters")
        return value


class AgentConfig(BaseModel):
    system_prompt: str = (
        "You resolve one production incident. Preserve evidence, make the narrowest fix, "
        "obey repository instructions and report every verification command."
    )

    @model_validator(mode="after")
    def system_prompt_is_required(self) -> AgentConfig:
        if not self.system_prompt.strip():
            raise ValueError("system_prompt cannot be blank")
        return self


class SafetyConfig(BaseModel):
    positive_goals: list[str] = Field(
        default_factory=lambda: [
            "Restore service health with the narrowest evidence-backed code change.",
            "Preserve incident evidence and document the verified root cause.",
            "Verify fixes locally and in the configured preview environment.",
        ]
    )
    negative_goals: list[str] = Field(
        default_factory=lambda: [
            "Do not expose, copy, or persist credentials or sensitive production data.",
            "Do not make direct changes to production systems or production data.",
            "Do not broaden the change beyond the current incident.",
        ]
    )
    guardrails: list[str] = Field(
        default_factory=lambda: [
            "Work only in the isolated repository worktree and within configured permissions.",
            "Stop when repository instructions or incident evidence conflict with a change.",
            "Never bypass required tests, review, or deployment verification.",
        ]
    )
    safeguards: list[str] = Field(
        default_factory=lambda: [
            "Establish reproducible evidence or a supported root cause before editing code.",
            "Run targeted tests and every relevant configured verification before publishing.",
            "Publish through a reviewable pull request and verify its exact deployed commit.",
        ]
    )


class GitHubConfig(BaseModel):
    webhook_secret_env: str = "GITHUB_WEBHOOK_SECRET"
    draft_pull_requests: bool = True
    agent_login: str = "incident-agent[bot]"
    agent_mention: str = "@incident-agent"
    allowed_author_associations: list[str] = Field(
        default_factory=lambda: ["OWNER", "MEMBER", "COLLABORATOR"]
    )


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(8765, ge=1, le=65535)
    public_url: str | None = None
    webhook_secret_env: str = "AGENT_WEBHOOK_SECRET"


class PlaywrightConfig(BaseModel):
    command: str = ""
    base_url_env: str = "PLAYWRIGHT_TEST_BASE_URL"
    timeout_seconds: int = Field(900, ge=1)
    retries: int = Field(1, ge=0)


class RepositoryConfig(BaseModel):
    name: str
    clone_url: str | None = None
    local_path: Path | None = None
    publish_mode: Literal["auto", "github", "local"] = "auto"
    base_branch: str = "main"
    incident_environments: list[str] = Field(default_factory=lambda: ["production"])
    verification_environment: str = "preview"
    project_instructions: str = "AGENTS.md"
    playwright: PlaywrightConfig = Field(default_factory=PlaywrightConfig)


class DeploymentConfig(BaseModel):
    reachability_timeout_seconds: int = Field(120, ge=1)
    poll_interval_seconds: float = Field(2, gt=0)


class ConnectorConfig(BaseModel):
    name: str
    purpose: Literal["incident", "output", "observability", "other"] = "other"
    type: Literal["mcp", "webhook"] = "mcp"
    transport: Literal["stdio", "streamable-http", "sse"] = "streamable-http"
    url: str | None = None
    command: list[str] = Field(default_factory=list)
    auth_token_env: str | None = None
    capabilities: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def transport_has_target(self) -> ConnectorConfig:
        if self.type == "mcp" and self.transport == "stdio" and not self.command:
            raise ValueError("stdio connectors require command")
        if self.type == "mcp" and self.transport != "stdio" and not self.url:
            raise ValueError("HTTP connectors require url")
        return self


class PermissionsConfig(BaseModel):
    mode: Literal["read-only", "workspace"] = "workspace"
    allow_dependency_installation: bool = True
    allow_migrations: bool = False
    allow_ci_modification: bool = False
    allow_snapshot_updates: bool = False
    allow_review_resolution: bool = True


class Config(BaseModel):
    runtime_root: Path = Path(".agent")
    max_concurrent_tasks: int = Field(2, ge=1)
    poll_interval_seconds: float = Field(2, gt=0)
    model: ModelConfig = Field(default_factory=ModelConfig)
    trigger: TriggerConfig = Field(default_factory=TriggerConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    deployment: DeploymentConfig = Field(default_factory=DeploymentConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    repositories: list[RepositoryConfig] = Field(default_factory=list)
    connectors: list[ConnectorConfig] = Field(default_factory=list)

    def repository(self, name: str) -> RepositoryConfig:
        for repository in self.repositories:
            if repository.name == name:
                return repository
        raise KeyError(f"repository is not configured: {name}")


def load_config(path: Path = Path(".agent/config.toml"), *, create: bool = True) -> Config:
    if not path.exists():
        config = Config(runtime_root=path.parent)
        if create:
            save_config(config, path)
        return config
    with path.open("rb") as handle:
        return Config.model_validate(tomllib.load(handle))


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"unsupported TOML value: {type(value).__name__}")


def _write_table(lines: list[str], name: str, values: dict[str, Any]) -> None:
    lines.append(f"[{name}]")
    for key, value in values.items():
        if not isinstance(value, dict) and value is not None:
            lines.append(f"{key} = {_toml_value(value)}")
    lines.append("")


def save_config(config: Config, path: Path = Path(".agent/config.toml")) -> None:
    """Persist configuration atomically. Config stores env names, never secret values."""
    data = config.model_dump(mode="python", exclude_none=True)
    lines: list[str] = []
    for key in ("runtime_root", "max_concurrent_tasks", "poll_interval_seconds"):
        lines.append(f"{key} = {_toml_value(data[key])}")
    lines.append("")
    for section in (
        "model",
        "trigger",
        "agent",
        "safety",
        "github",
        "server",
        "deployment",
        "permissions",
    ):
        _write_table(lines, section, data[section])
    for repository in data["repositories"]:
        playwright = repository.pop("playwright")
        lines.append("[[repositories]]")
        for key, value in repository.items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
        _write_table(lines, "repositories.playwright", playwright)
    for connector in data["connectors"]:
        lines.append("[[connectors]]")
        for key, value in connector.items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(path)
