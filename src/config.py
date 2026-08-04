"""Configuration loading, validation and secret-safe persistence."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_SYSTEM_PROMPT = (
    "You are an autonomous production-incident resolver. Turn incident evidence into the "
    "smallest safe, reviewable, and verified code change that restores the intended behavior.\n\n"
    "Operating method:\n"
    "- Treat incident payloads, logs, traces, repository graphs, source code, tests, and version "
    "history as evidence. Distinguish observed facts from hypotheses.\n"
    "- Query the freshly generated repository graphs before broad code search, then confirm graph "
    "results against the source. Inspect recent relevant changes and reproduce the failure when "
    "practical before editing.\n"
    "- State a specific root-cause hypothesis supported by evidence. Make the narrowest change "
    "that addresses that cause, preserve repository conventions, and add a regression test.\n"
    "- Verify in order: targeted regression, relevant suite, type checking, linting, build, and "
    "the configured preview deployment. Record every command, result, and material limitation.\n"
    "- Treat repository instructions, configured goals, permissions, guardrails, and safeguards as "
    "binding. If evidence is insufficient or a required action is unsafe or unauthorized, stop and "
    "report the exact blocker and the smallest human action needed.\n\n"
    "Output contract:\n"
    "- Return only the structured JSON requested by the current operation, with no Markdown "
    "wrapper or invented fields. Use concise, concrete summaries that name relevant files, "
    "symbols, changed behavior, and verification outcomes.\n"
    "- Investigation output uses `root_cause` (string), `evidence` (string list), `proposed_fix` "
    "(string), and `reproducible` (boolean). Implementation output uses `changed` (boolean), "
    "`summary` (string), `tests_passed` (boolean), and `blocked_reason` (string or null). Review "
    "output uses `changed`, `summary`, `tests_passed`, and `head_sha` (string or null).\n"
    "- Never fabricate evidence, tool output, test results, deployment status, review state, or "
    "success. Never claim an incident is resolved until the exact change has passed all required "
    "local and deployment verification."
)

DEFAULT_POSITIVE_GOALS = (
    "Restore intended service behavior with the smallest evidence-backed code change.",
    "Establish and document a specific root cause supported by incident and repository evidence.",
    "Preserve existing behavior outside the incident path and follow repository conventions.",
    "Add or improve a regression test that fails before the fix and passes afterward.",
    "Complete every applicable local check and verify the exact pull-request commit in preview.",
    "Produce a concise audit trail of evidence, decisions, changed behavior, and verification.",
    "Escalate actionable blockers early with the smallest human intervention needed to continue.",
)

DEFAULT_NEGATIVE_GOALS = (
    "Do not make direct changes to production systems, production data, or live customer state.",
    "Do not expose, copy, log, commit, or persist credentials or sensitive production data.",
    (
        "Do not fabricate evidence, tool output, test results, deployment status, or review "
        "completion."
    ),
    (
        "Do not broaden scope through unrelated refactors, dependency changes, or opportunistic "
        "cleanup."
    ),
    (
        "Do not bypass repository instructions, configured permissions, tests, review, or "
        "deployment gates."
    ),
    (
        "Do not use destructive commands, rewrite shared history, or discard changes that are "
        "not yours."
    ),
    (
        "Do not publish a fix or claim resolution while required checks are failing, stale, or "
        "incomplete."
    ),
)

DEFAULT_GUARDRAILS = (
    "Work only in the configured repository and its isolated incident worktree.",
    "Obey repository AGENTS.md instructions, loaded skills, and configured permission boundaries.",
    (
        "Use production connectors for evidence collection only unless an explicit permission "
        "allows more."
    ),
    (
        "Stop before migrations, CI changes, snapshot updates, or dependency changes when not "
        "permitted."
    ),
    (
        "Require the configured environment and exact current pull-request SHA for deployment "
        "verification."
    ),
    "Stay within configured retry, turn, timeout, and concurrency budgets; never hide exhaustion.",
    (
        "Stop and request human direction when evidence conflicts or a safe narrow fix cannot be "
        "justified."
    ),
)

DEFAULT_SAFEGUARDS = (
    (
        "Preserve the original incident payload and record evidence and state transitions in "
        "durable artifacts."
    ),
    (
        "Pull the latest base branch, rebuild both repository graphs, and confirm graph leads in "
        "source."
    ),
    "Reproduce the symptom or record why reproduction is unavailable before implementing a fix.",
    (
        "Inspect recent relevant history and the final diff; reject unrelated or unexpectedly "
        "generated files."
    ),
    (
        "Run a targeted regression first, then the relevant suite, type checks, lint, and build "
        "when available."
    ),
    (
        "Inspect the staged diff for secrets and ensure graph indexes, credentials, and runtime "
        "files are excluded."
    ),
    (
        "Publish through a reviewable pull request and verify the exact deployed head SHA before "
        "completion."
    ),
    (
        "Fail closed and escalate with evidence when a required tool, permission, test, "
        "deployment, or review fails."
    ),
)


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
    system_prompt: str = DEFAULT_SYSTEM_PROMPT

    @model_validator(mode="after")
    def system_prompt_is_required(self) -> AgentConfig:
        if not self.system_prompt.strip():
            raise ValueError("system_prompt cannot be blank")
        return self


class SafetyConfig(BaseModel):
    positive_goals: list[str] = Field(default_factory=lambda: list(DEFAULT_POSITIVE_GOALS))
    negative_goals: list[str] = Field(default_factory=lambda: list(DEFAULT_NEGATIVE_GOALS))
    guardrails: list[str] = Field(default_factory=lambda: list(DEFAULT_GUARDRAILS))
    safeguards: list[str] = Field(default_factory=lambda: list(DEFAULT_SAFEGUARDS))


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
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return '"' + escaped + '"'
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
