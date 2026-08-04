"""Textual configuration editor for the complete incident harness."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from .config import (
    Config,
    ConnectorConfig,
    PlaywrightConfig,
    RepositoryConfig,
    load_config,
    save_config,
)
from .connectors import ConnectorManager
from .tooling import (
    CommandRunner,
    GitHubRepository,
    clone_and_index_repository,
    github_login,
    list_github_repositories,
    repository_name_from_url,
    repository_slug,
)


def _options(*values: str) -> list[tuple[str, str]]:
    return [(value.replace("-", " ").title(), value) for value in values]


def _lines(value: list[str]) -> str:
    return "\n".join(value)


class ConfigurationApp(App[None]):
    """A tabbed, form-based editor that never asks for secret values."""

    CSS = """
    Screen { background: $surface; }
    #content { height: 1fr; }
    .page { padding: 1 2; }
    .section { height: auto; border: round $primary-darken-2; padding: 1; margin-bottom: 1; }
    .card { height: auto; border: round $secondary-darken-2; padding: 1; margin-bottom: 1; }
    .row { height: auto; }
    .row Input, .row Select { width: 1fr; margin-right: 1; }
    Input, Select, TextArea { margin-bottom: 1; }
    TextArea { height: 7; }
    Checkbox { margin-bottom: 1; }
    Label { color: $text-muted; }
    Button { margin-right: 1; margin-bottom: 1; }
    #repositories-list, #connectors-list { height: auto; }
    .inline-status { height: auto; min-height: 1; color: $text-muted; margin-bottom: 1; }
    #status { height: 3; padding: 1 2; }
    #model-help { height: auto; color: $text-muted; margin-bottom: 1; }
    """

    def __init__(
        self,
        path: Path = Path(".agent/config.toml"),
        *,
        command_runner: CommandRunner = subprocess.run,
    ) -> None:
        super().__init__()
        self.path = path
        self.command_runner = command_runner
        self.config = load_config(path)
        self._next_collection_id = 0
        self._repository_keys: list[str] = []
        self._connector_keys: list[str] = []
        self._github_repositories: dict[str, dict[str, GitHubRepository]] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(id="content", initial="model-tab"):
            with TabPane("Model", id="model-tab"):
                yield VerticalScroll(*self._model_page(), classes="page")
            with TabPane("Runtime", id="runtime-tab"):
                yield VerticalScroll(*self._runtime_page(), classes="page")
            with TabPane("Repositories", id="repositories-tab"):
                yield VerticalScroll(*self._repositories_page(), classes="page")
            with TabPane("Connections", id="connections-tab"):
                yield VerticalScroll(*self._connections_page(), classes="page")
            with TabPane("Safety", id="safety-tab"):
                yield VerticalScroll(*self._safety_page(), classes="page")
        # Validation errors can contain Pydantic markup such as ``[type=...]``. Render the
        # status as plain text so an invalid draft reports the error instead of crashing TUI.
        yield Static("", id="status", markup=False)
        yield Horizontal(
            Button("Save configuration", id="save", variant="primary"),
            Button("Quit", id="quit"),
            classes="row",
        )
        yield Footer()

    @staticmethod
    def _field(label: str, widget: Any) -> Vertical:
        return Vertical(Label(label), widget, classes="section")

    @staticmethod
    def _input(value: Any, field_id: str, *, placeholder: str = "") -> Input:
        return Input(
            value="" if value is None else str(value), id=field_id, placeholder=placeholder
        )

    @staticmethod
    def _select(
        value: str, field_id: str, values: tuple[str, ...], *, prompt: str = "Select"
    ) -> Select:
        return Select(_options(*values), value=value, allow_blank=False, prompt=prompt, id=field_id)

    def _model_page(self) -> list[Any]:
        model = self.config.model
        return [
            Static(
                "Configure any provider exposing an OpenAI-compatible chat-completions API. "
                "Secrets are referenced by environment-variable name and are never saved here."
            ),
            Vertical(
                Label("Execution location"),
                self._select(model.mode, "model-mode", ("local", "remote")),
                Static(self._model_help(model.mode), id="model-help"),
                classes="section",
            ),
            Vertical(
                Label("Provider label (for your records)"),
                self._input(model.provider, "provider", placeholder="ollama, vllm, openai, ..."),
                Label("Model name"),
                self._input(model.name, "model", placeholder="the model identifier"),
                Label("OpenAI-compatible base URL (include /v1 when required)"),
                self._input(
                    model.base_url, "model-base-url", placeholder="https://api.example.com/v1"
                ),
                Label("API key environment variable (optional for local servers)"),
                self._input(model.api_key_env, "model-api-key-env", placeholder="OPENAI_API_KEY"),
                Label("Organization environment variable (optional)"),
                self._input(model.organization_env, "model-organization-env"),
                classes="section",
            ),
            Vertical(
                Label("Reasoning preference"),
                self._input(model.reasoning, "model-reasoning", placeholder="high"),
                Label("Temperature (blank uses provider default)"),
                self._input(model.temperature, "model-temperature"),
                Label("Top P (blank uses provider default)"),
                self._input(model.top_p, "model-top-p"),
                Label("Maximum output tokens (blank uses provider default)"),
                self._input(model.max_tokens, "model-max-tokens"),
                Checkbox(
                    "Allow parallel tool calls",
                    value=model.parallel_tool_calls,
                    id="model-parallel-tools",
                ),
                Checkbox(
                    "Show live model execution details in the terminal",
                    value=model.show_execution_details,
                    id="model-show-execution-details",
                ),
                Label("Maximum turns per agent iteration"),
                self._input(model.max_turns_per_iteration, "model-max-turns"),
                Label("Maximum retry iterations per task"),
                self._input(model.max_task_iterations, "model-max-iterations"),
                Label("Tool timeout in seconds"),
                self._input(model.tool_timeout_seconds, "model-tool-timeout"),
                classes="section",
            ),
        ]

    @staticmethod
    def _model_help(mode: str) -> str:
        if mode == "local":
            return (
                "Local mode is intended for Ollama, vLLM, LM Studio, or another on-device endpoint."
            )
        return (
            "Remote mode uses a hosted endpoint; configure its base URL and API-key "
            "environment variable."
        )

    def _runtime_page(self) -> list[Any]:
        trigger = self.config.trigger
        server = self.config.server
        github = self.config.github
        deployment = self.config.deployment
        permissions = self.config.permissions
        return [
            Vertical(
                Label("How incidents trigger the harness"),
                self._select(trigger.mode, "trigger-mode", ("hook", "workflow", "agent-call")),
                Label("Incident hook path"),
                self._input(trigger.hook_path, "trigger-hook-path"),
                Label("Workflow name (when using workflow mode)"),
                self._input(trigger.workflow_name, "trigger-workflow-name"),
                Label("Agent name (when using agent-call mode)"),
                self._input(trigger.agent_name, "trigger-agent-name"),
                Checkbox(
                    "Require an explicit acknowledgement",
                    value=trigger.require_ack,
                    id="trigger-require-ack",
                ),
                classes="section",
            ),
            Vertical(
                Label("Runtime root"),
                self._input(self.config.runtime_root, "runtime-root"),
                Label("Maximum concurrent tasks"),
                self._input(self.config.max_concurrent_tasks, "max-concurrent-tasks"),
                Label("Worker poll interval in seconds"),
                self._input(self.config.poll_interval_seconds, "worker-poll-interval"),
                classes="section",
            ),
            Vertical(
                Label("HTTP host"),
                self._input(server.host, "host"),
                Label("HTTP port"),
                self._input(server.port, "port"),
                Label("Public URL (optional)"),
                self._input(server.public_url, "public-url"),
                Label("Agent webhook secret environment variable"),
                self._input(server.webhook_secret_env, "server-webhook-secret-env"),
                classes="section",
            ),
            Vertical(
                Label("GitHub webhook secret environment variable"),
                self._input(github.webhook_secret_env, "github-webhook-secret-env"),
                Label("Agent GitHub login"),
                self._input(github.agent_login, "github-agent-login"),
                Label("Agent mention"),
                self._input(github.agent_mention, "github-agent-mention"),
                Label("Allowed author associations (comma-separated)"),
                self._input(
                    ", ".join(github.allowed_author_associations), "github-author-associations"
                ),
                Checkbox(
                    "Create draft pull requests",
                    value=github.draft_pull_requests,
                    id="github-draft-prs",
                ),
                classes="section",
            ),
            Vertical(
                Label("Deployment reachability timeout in seconds"),
                self._input(deployment.reachability_timeout_seconds, "deployment-timeout"),
                Label("Deployment poll interval in seconds"),
                self._input(deployment.poll_interval_seconds, "deployment-poll-interval"),
                classes="section",
            ),
            Vertical(
                Label("Workspace permission mode"),
                self._select(permissions.mode, "permissions-mode", ("read-only", "workspace")),
                Checkbox(
                    "Allow dependency installation",
                    value=permissions.allow_dependency_installation,
                    id="allow-dependency-installation",
                ),
                Checkbox(
                    "Allow database migrations",
                    value=permissions.allow_migrations,
                    id="allow-migrations",
                ),
                Checkbox(
                    "Allow CI modifications",
                    value=permissions.allow_ci_modification,
                    id="allow-ci-modification",
                ),
                Checkbox(
                    "Allow snapshot updates",
                    value=permissions.allow_snapshot_updates,
                    id="allow-snapshot-updates",
                ),
                Checkbox(
                    "Allow resolving review comments",
                    value=permissions.allow_review_resolution,
                    id="allow-review-resolution",
                ),
                classes="section",
            ),
        ]

    def _repositories_page(self) -> list[Any]:
        forms: list[Any] = []
        for repository in self.config.repositories:
            key = self._new_key("repository")
            self._repository_keys.append(key)
            forms.append(self._repository_form(key, repository))
        container = Vertical(*forms, id="repositories-list")
        return [
            Static(
                "Repositories define local/remote source, publishing, incident environments, "
                "and deployment verification."
            ),
            container,
            Button("Add repository", id="add-repository"),
        ]

    def _connections_page(self) -> list[Any]:
        forms: list[Any] = []
        for connector in self.config.connectors:
            key = self._new_key("connector")
            self._connector_keys.append(key)
            forms.append(self._connector_form(key, connector))
        container = Vertical(*forms, id="connectors-list")
        return [
            Static(
                "Use purpose to distinguish incident intake, PR output, and "
                "observability/logging MCP connections."
            ),
            container,
            Button("Add connection", id="add-connector"),
        ]

    def _safety_page(self) -> list[Any]:
        safety = self.config.safety
        return [
            Static(
                "The system prompt and safety contract below are included in every agent run. "
                "Keep them concrete and operational."
            ),
            Vertical(
                Label("System prompt"),
                TextArea(self.config.agent.system_prompt, id="system-prompt"),
                Label("Positive goals — one per line"),
                TextArea(_lines(safety.positive_goals), id="positive-goals"),
                Label("Negative goals — one per line"),
                TextArea(_lines(safety.negative_goals), id="negative-goals"),
                Label("Guardrails — conditions the agent must not cross"),
                TextArea(_lines(safety.guardrails), id="guardrails"),
                Label("Safeguards — checks or approvals required before risky actions"),
                TextArea(_lines(safety.safeguards), id="safeguards"),
                classes="section",
            ),
        ]

    def _new_key(self, prefix: str) -> str:
        key = f"{prefix}-{self._next_collection_id}"
        self._next_collection_id += 1
        return key

    def _repository_form(self, key: str, repository: RepositoryConfig | None = None) -> Vertical:
        if repository is None:
            repository = RepositoryConfig.model_construct(
                name="",
                clone_url=None,
                local_path=None,
                publish_mode="auto",
                base_branch="main",
                incident_environments=["production"],
                verification_environment="preview",
                project_instructions="AGENTS.md",
                playwright=PlaywrightConfig(),
            )
        playwright = repository.playwright
        prefix = f"repo-{key}"
        source = (
            "github" if repository.clone_url and "github.com" in repository.clone_url else "url"
        )
        return Vertical(
            Label("Repository"),
            self._input(repository.name, f"{prefix}-name", placeholder="owner/repository"),
            Label("Get repository from"),
            self._select(source, f"{prefix}-source", ("github", "url")),
            Label("GitHub repository"),
            Select(
                [],
                allow_blank=True,
                prompt="Log in to GitHub to load repositories",
                id=f"{prefix}-github-repository",
            ),
            Button("Log in to GitHub and load repositories", id=f"github-login-{key}"),
            Label("Clone URL"),
            self._input(repository.clone_url, f"{prefix}-clone-url"),
            Button("Clone/pull and build graphs", id=f"setup-{key}", variant="primary"),
            Static("", id=f"repo-status-{key}", classes="inline-status", markup=False),
            Label("Managed local checkout (populated after cloning; existing paths are supported)"),
            self._input(repository.local_path, f"{prefix}-local-path"),
            Label("Publish mode"),
            self._select(
                repository.publish_mode, f"{prefix}-publish-mode", ("auto", "github", "local")
            ),
            Label("Base branch"),
            self._input(repository.base_branch, f"{prefix}-base-branch"),
            Label("Incident environments (comma-separated)"),
            self._input(
                ", ".join(repository.incident_environments), f"{prefix}-incident-environments"
            ),
            Label("Verification environment"),
            self._input(repository.verification_environment, f"{prefix}-verification-environment"),
            Label("Project instructions filename"),
            self._input(repository.project_instructions, f"{prefix}-project-instructions"),
            Label("Playwright command (blank disables deployment browser checks)"),
            self._input(playwright.command, f"{prefix}-playwright-command"),
            Label("Playwright base URL environment variable"),
            self._input(playwright.base_url_env, f"{prefix}-playwright-base-url-env"),
            Label("Playwright timeout seconds"),
            self._input(playwright.timeout_seconds, f"{prefix}-playwright-timeout"),
            Label("Playwright retries"),
            self._input(playwright.retries, f"{prefix}-playwright-retries"),
            Button("Remove repository", id=f"remove-{key}"),
            id=key,
            classes="card",
        )

    def _connector_form(self, key: str, connector: ConnectorConfig | None = None) -> Vertical:
        if connector is None:
            # A newly added form is an incomplete draft. Keep the model validator strict for
            # persisted/runtime connectors, but don't require a URL before the user can fill in
            # the form.
            connector = ConnectorConfig.model_construct(
                name="new-connection",
                purpose="other",
                type="mcp",
                transport="streamable-http",
                url=None,
                command=[],
                auth_token_env=None,
                capabilities=[],
            )
        prefix = f"connector-{key}"
        return Vertical(
            Label("Connection name"),
            self._input(connector.name, f"{prefix}-name"),
            Label("Purpose"),
            self._select(
                connector.purpose,
                f"{prefix}-purpose",
                ("incident", "output", "observability", "other"),
            ),
            Label("Type"),
            self._select(connector.type, f"{prefix}-type", ("mcp", "webhook")),
            Label("MCP transport"),
            self._select(
                connector.transport, f"{prefix}-transport", ("stdio", "streamable-http", "sse")
            ),
            Label("URL (HTTP/SSE MCP)"),
            self._input(connector.url, f"{prefix}-url"),
            Label("Command (stdio MCP; space-separated)"),
            self._input(" ".join(connector.command), f"{prefix}-command"),
            Label("Auth token environment variable"),
            self._input(connector.auth_token_env, f"{prefix}-auth-token-env"),
            Label("Capabilities (comma-separated)"),
            self._input(", ".join(connector.capabilities), f"{prefix}-capabilities"),
            Button("Test connection", id=f"test-{key}"),
            Static("", id=f"connector-status-{key}", classes="inline-status", markup=False),
            Button("Remove connection", id=f"remove-{key}"),
            id=key,
            classes="card",
        )

    def _value(self, field_id: str, widget_type: type[Any] = Input) -> Any:
        return self.query_one(f"#{field_id}", widget_type).value

    def _text(self, field_id: str) -> str:
        return self.query_one(f"#{field_id}", TextArea).text

    def _number(self, field_id: str, *, integer: bool = False) -> int | float:
        raw = self._value(field_id).strip()
        if not raw:
            raise ValueError(f"{field_id} cannot be blank")
        try:
            return int(raw) if integer else float(raw)
        except ValueError as error:
            raise ValueError(f"{field_id} must be a number") from error

    def _optional_number(self, field_id: str, *, integer: bool = False) -> int | float | None:
        raw = self._value(field_id).strip()
        if not raw:
            return None
        return self._number(field_id, integer=integer)

    def _checked(self, field_id: str) -> bool:
        return self.query_one(f"#{field_id}", Checkbox).value

    def _selected(self, field_id: str) -> str:
        value = self.query_one(f"#{field_id}", Select).value
        if value is Select.NULL:
            raise ValueError(f"{field_id} must be selected")
        return str(value)

    @staticmethod
    def _split(value: str) -> list[str]:
        return [part.strip() for part in value.split(",") if part.strip()]

    @staticmethod
    def _lines_from(value: str) -> list[str]:
        return [line.strip() for line in value.splitlines() if line.strip()]

    def _collect(self) -> Config:
        model = self.config.model.model_copy(
            update={
                "mode": self._selected("model-mode"),
                "provider": self._value("provider").strip(),
                "name": self._value("model").strip(),
                "base_url": self._value("model-base-url").strip() or None,
                "api_key_env": self._value("model-api-key-env").strip(),
                "organization_env": self._value("model-organization-env").strip() or None,
                "reasoning": self._value("model-reasoning").strip() or None,
                "temperature": self._optional_number("model-temperature"),
                "top_p": self._optional_number("model-top-p"),
                "max_tokens": self._optional_number("model-max-tokens", integer=True),
                "parallel_tool_calls": self._checked("model-parallel-tools"),
                "show_execution_details": self._checked("model-show-execution-details"),
                "max_turns_per_iteration": self._number("model-max-turns", integer=True),
                "max_task_iterations": self._number("model-max-iterations", integer=True),
                "tool_timeout_seconds": self._number("model-tool-timeout", integer=True),
            }
        )
        trigger = self.config.trigger.model_copy(
            update={
                "mode": self._selected("trigger-mode"),
                "hook_path": self._value("trigger-hook-path").strip(),
                "workflow_name": self._value("trigger-workflow-name").strip(),
                "agent_name": self._value("trigger-agent-name").strip(),
                "require_ack": self._checked("trigger-require-ack"),
            }
        )
        safety = self.config.safety.model_copy(
            update={
                "positive_goals": self._lines_from(self._text("positive-goals")),
                "negative_goals": self._lines_from(self._text("negative-goals")),
                "guardrails": self._lines_from(self._text("guardrails")),
                "safeguards": self._lines_from(self._text("safeguards")),
            }
        )
        agent = self.config.agent.model_copy(
            update={"system_prompt": self._text("system-prompt").strip()}
        )
        server = self.config.server.model_copy(
            update={
                "host": self._value("host").strip(),
                "port": self._number("port", integer=True),
                "public_url": self._value("public-url").strip() or None,
                "webhook_secret_env": self._value("server-webhook-secret-env").strip(),
            }
        )
        github = self.config.github.model_copy(
            update={
                "webhook_secret_env": self._value("github-webhook-secret-env").strip(),
                "draft_pull_requests": self._checked("github-draft-prs"),
                "agent_login": self._value("github-agent-login").strip(),
                "agent_mention": self._value("github-agent-mention").strip(),
                "allowed_author_associations": self._split(
                    self._value("github-author-associations")
                ),
            }
        )
        deployment = self.config.deployment.model_copy(
            update={
                "reachability_timeout_seconds": self._number("deployment-timeout", integer=True),
                "poll_interval_seconds": self._number("deployment-poll-interval"),
            }
        )
        permissions = self.config.permissions.model_copy(
            update={
                "mode": self._selected("permissions-mode"),
                "allow_dependency_installation": self._checked("allow-dependency-installation"),
                "allow_migrations": self._checked("allow-migrations"),
                "allow_ci_modification": self._checked("allow-ci-modification"),
                "allow_snapshot_updates": self._checked("allow-snapshot-updates"),
                "allow_review_resolution": self._checked("allow-review-resolution"),
            }
        )
        repositories = [self._collect_repository(key) for key in self._repository_keys]
        connectors = [self._collect_connector(key) for key in self._connector_keys]
        return Config(
            runtime_root=self._value("runtime-root").strip(),
            max_concurrent_tasks=self._number("max-concurrent-tasks", integer=True),
            poll_interval_seconds=self._number("worker-poll-interval"),
            model=model,
            trigger=trigger,
            agent=agent,
            safety=safety,
            server=server,
            github=github,
            deployment=deployment,
            permissions=permissions,
            repositories=repositories,
            connectors=connectors,
        )

    def _collect_repository(self, key: str) -> RepositoryConfig:
        prefix = f"repo-{key}"
        clone_url = self._value(f"{prefix}-clone-url").strip() or None
        local_path = self._value(f"{prefix}-local-path").strip() or None
        name = self._value(f"{prefix}-name").strip()
        if not name and clone_url:
            name = repository_name_from_url(clone_url)
        if not name:
            raise ValueError("repository name cannot be blank")
        repository_slug(name)
        if not clone_url and not local_path:
            raise ValueError(
                f"repository {name} must be selected from GitHub or configured with a clone URL"
            )
        return RepositoryConfig(
            name=name,
            clone_url=clone_url,
            local_path=local_path,
            publish_mode=self._selected(f"{prefix}-publish-mode"),
            base_branch=self._value(f"{prefix}-base-branch").strip(),
            incident_environments=self._split(self._value(f"{prefix}-incident-environments")),
            verification_environment=self._value(f"{prefix}-verification-environment").strip(),
            project_instructions=self._value(f"{prefix}-project-instructions").strip(),
            playwright=PlaywrightConfig(
                command=self._value(f"{prefix}-playwright-command"),
                base_url_env=self._value(f"{prefix}-playwright-base-url-env").strip(),
                timeout_seconds=self._number(f"{prefix}-playwright-timeout", integer=True),
                retries=self._number(f"{prefix}-playwright-retries", integer=True),
            ),
        )

    def _collect_connector(self, key: str) -> ConnectorConfig:
        prefix = f"connector-{key}"
        command = self._value(f"{prefix}-command").strip()
        return ConnectorConfig(
            name=self._value(f"{prefix}-name").strip(),
            purpose=self._selected(f"{prefix}-purpose"),
            type=self._selected(f"{prefix}-type"),
            transport=self._selected(f"{prefix}-transport"),
            url=self._value(f"{prefix}-url").strip() or None,
            command=command.split(),
            auth_token_env=self._value(f"{prefix}-auth-token-env").strip() or None,
            capabilities=self._split(self._value(f"{prefix}-capabilities")),
        )

    def _set_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    def on_mount(self) -> None:
        for key in self._repository_keys:
            self._set_repository_source(key)

    def _set_repository_source(self, key: str) -> None:
        prefix = f"repo-{key}"
        github = self._selected(f"{prefix}-source") == "github"
        self.query_one(f"#{prefix}-github-repository", Select).disabled = not github
        self.query_one(f"#github-login-{key}", Button).disabled = not github
        self.query_one(f"#{prefix}-clone-url", Input).disabled = github

    @staticmethod
    def _result_message(stdout: str, stderr: str, fallback: str) -> str:
        return (stderr or stdout).strip() or fallback

    async def _load_github_repositories(self, key: str) -> None:
        status = self.query_one(f"#repo-status-{key}", Static)
        status.update("Opening GitHub login. Complete authentication in your browser…")
        login = await asyncio.to_thread(github_login, runner=self.command_runner)
        if not login.succeeded:
            status.update(self._result_message(login.stdout, login.stderr, "GitHub login failed"))
            return
        status.update("Loading repositories from GitHub…")
        result, repositories = await asyncio.to_thread(
            list_github_repositories, runner=self.command_runner
        )
        if not result.succeeded:
            status.update(
                self._result_message(result.stdout, result.stderr, "Could not load repositories")
            )
            return
        self._github_repositories[key] = {
            repository.name: repository for repository in repositories
        }
        select = self.query_one(f"#repo-{key}-github-repository", Select)
        select.set_options([(repository.name, repository.name) for repository in repositories])
        status.update(f"Loaded {len(repositories)} GitHub repositories. Select one above.")

    async def _setup_repository(self, key: str) -> None:
        prefix = f"repo-{key}"
        status = self.query_one(f"#repo-status-{key}", Static)
        clone_url = self._value(f"{prefix}-clone-url").strip()
        name = self._value(f"{prefix}-name").strip()
        if not clone_url:
            status.update("Select a GitHub repository or enter a clone URL first.")
            return
        try:
            name = name or repository_name_from_url(clone_url)
            destination = (
                Path(self._value("runtime-root").strip()).expanduser()
                / "repositories"
                / repository_slug(name)
            )
        except ValueError as error:
            status.update(str(error))
            return
        self.query_one(f"#{prefix}-name", Input).value = name
        status.update("Cloning/pulling the repository, then building both code graphs…")
        try:
            result = await asyncio.to_thread(
                clone_and_index_repository,
                clone_url,
                destination,
                runner=self.command_runner,
            )
        except (FileExistsError, OSError) as error:
            status.update(str(error))
            return
        if not result.acquisition.succeeded:
            status.update(
                self._result_message(
                    result.acquisition.stdout,
                    result.acquisition.stderr,
                    "Repository clone/pull failed",
                )
            )
            return
        failures = [
            graph
            for graph in (result.graphify, result.code_review_graph)
            if graph is None or not graph.succeeded
        ]
        if failures:
            messages = [
                "graph command did not run"
                if graph is None
                else self._result_message(graph.stdout, graph.stderr, str(graph.command[0]))
                for graph in failures
            ]
            status.update("Graph generation failed: " + "; ".join(messages))
            return
        self.query_one(f"#{prefix}-local-path", Input).value = str(result.path)
        try:
            self.config = self._collect()
            save_config(self.config, self.path)
            status.update(f"Ready: pulled, indexed, and saved {name}.")
            self._set_status(f"Saved {self.path}")
        except (TypeError, ValueError) as error:
            status.update(f"Repository is indexed; correct the remaining form error: {error}")

    async def _test_connector(self, key: str) -> None:
        status = self.query_one(f"#connector-status-{key}", Static)
        try:
            connector = self._collect_connector(key)
        except (TypeError, ValueError) as error:
            status.update(f"Invalid connection: {error}")
            return
        status.update("Testing connection…")
        result = await ConnectorManager([connector]).test_connection(connector.name)
        prefix = "Connected" if result.connected else "Connection failed"
        status.update(f"{prefix}: {result.message}")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "model-mode" and event.value is not Select.NULL:
            self.query_one("#model-help", Static).update(self._model_help(str(event.value)))
            return
        select_id = event.select.id or ""
        if select_id.startswith("repo-") and select_id.endswith("-source"):
            key = select_id.removeprefix("repo-").removesuffix("-source")
            if event.value is not Select.NULL:
                self._set_repository_source(key)
            return
        for key, repositories in self._github_repositories.items():
            if select_id != f"repo-{key}-github-repository" or event.value is Select.NULL:
                continue
            repository = repositories[str(event.value)]
            prefix = f"repo-{key}"
            self.query_one(f"#{prefix}-name", Input).value = repository.name
            self.query_one(f"#{prefix}-clone-url", Input).value = repository.clone_url
            self.query_one(f"#{prefix}-base-branch", Input).value = repository.base_branch
            return

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "quit":
            self.exit()
            return
        if button_id == "add-repository":
            key = self._new_key("repository")
            self._repository_keys.append(key)
            await self.query_one("#repositories-list", Vertical).mount(self._repository_form(key))
            self._set_repository_source(key)
            return
        if button_id == "add-connector":
            key = self._new_key("connector")
            self._connector_keys.append(key)
            await self.query_one("#connectors-list", Vertical).mount(self._connector_form(key))
            return
        if button_id.startswith("github-login-repository-"):
            await self._load_github_repositories(button_id.removeprefix("github-login-"))
            return
        if button_id.startswith("setup-repository-"):
            await self._setup_repository(button_id.removeprefix("setup-"))
            return
        if button_id.startswith("test-connector-"):
            await self._test_connector(button_id.removeprefix("test-"))
            return
        if button_id.startswith("remove-repository-") or button_id.startswith("remove-connector-"):
            key = button_id.removeprefix("remove-")
            if key.startswith("repository-"):
                self._repository_keys.remove(key)
            else:
                self._connector_keys.remove(key)
            await self.query_one(f"#{key}").remove()
            return
        if button_id != "save":
            return
        try:
            self.config = self._collect()
            save_config(self.config, self.path)
            self._set_status(f"Saved {self.path}")
        except (TypeError, ValueError) as error:
            self._set_status(f"Invalid configuration: {error}")


def run_tui(path: Path = Path(".agent/config.toml")) -> None:
    ConfigurationApp(path).run()
