"""Bounded coding-agent facade with durable context assembly."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .config import Config
from .models import (
    FixResult,
    InvestigationResult,
    ReviewComment,
    ReviewResult,
    TaskEvent,
    TaskRecord,
)
from .storage import Storage
from .tools import WorkspaceTools

AgentBackend = Callable[[str, str, WorkspaceTools, list[Any]], Awaitable[dict[str, Any]]]


class OpenAIAgentsBackend:
    """Default runtime backed by the OpenAI Agents SDK.

    Alternative and local providers remain injectable through ``AgentBackend``;
    OpenAI-compatible local endpoints can also be selected through the SDK's
    standard environment configuration.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._compatible_client: Any | None = None

    def _uses_compatible_client(self) -> bool:
        model = self.config.model
        return bool(
            model.base_url or model.api_key_env != "OPENAI_API_KEY" or model.organization_env
        )

    def _model(self, agents_module: Any) -> str | Any:
        """Build the SDK model for any OpenAI-compatible chat-completions endpoint."""
        model_config = self.config.model
        if not self._uses_compatible_client():
            return model_config.name

        api_key = os.getenv(model_config.api_key_env) if model_config.api_key_env else None
        # Local servers commonly ignore the key, while AsyncOpenAI still requires a
        # non-empty value unless credential enforcement is disabled.
        if not api_key and model_config.mode == "local":
            api_key = "local"
        organization = (
            os.getenv(model_config.organization_env) if model_config.organization_env else None
        )
        if self._compatible_client is None:
            self._compatible_client = agents_module.AsyncOpenAI(
                api_key=api_key,
                organization=organization,
                base_url=model_config.base_url,
            )
        return agents_module.OpenAIChatCompletionsModel(
            model=model_config.name,
            openai_client=self._compatible_client,
        )

    async def __call__(
        self,
        instructions: str,
        prompt: str,
        workspace: WorkspaceTools,
        connector_tools: list[Any],
    ) -> dict[str, Any]:
        from agents import Agent, ModelSettings, Runner, function_tool
        from openai.types.shared import Reasoning

        agents_module: Any = None
        if self._uses_compatible_client():
            from agents import AsyncOpenAI, OpenAIChatCompletionsModel

            # Keep the imports on the SDK module for simple test doubles and
            # older agents releases, while still passing the compatible client
            # to the provider-aware model when a base URL is configured.
            agents_module = type(
                "AgentsModule",
                (),
                {
                    "AsyncOpenAI": AsyncOpenAI,
                    "OpenAIChatCompletionsModel": OpenAIChatCompletionsModel,
                },
            )

        @function_tool
        async def shell(command: str) -> str:
            """Run a shell command in the isolated repository worktree."""
            result = await workspace.shell(command)
            return json.dumps(
                {
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "truncated": result.truncated,
                }
            )

        @function_tool
        def read_file(path: str) -> str:
            """Read one UTF-8 file relative to the repository root."""
            return workspace.read_file(path)

        @function_tool
        def write_file(path: str, content: str) -> str:
            """Write one UTF-8 file relative to the repository root."""
            workspace.write_file(path, content)
            return "written"

        @function_tool
        def replace_in_file(path: str, old: str, new: str) -> str:
            """Replace exactly one occurrence in a repository file."""
            workspace.replace_in_file(path, old, new)
            return "replaced"

        @function_tool
        def rg_conversation_history(pattern: str, limit: int = 20) -> str:
            """Search prior messages for this incident with a ripgrep regular expression.

            Use this when asked what was done or why and the current context does not contain
            enough evidence. Matching messages are loaded into the current context as JSON.
            """
            return workspace.rg_conversation_history(pattern, limit)

        @function_tool
        async def graphify_query(question: str, budget: int = 2000) -> str:
            """Query the freshly generated semantic/structural repository graph."""
            graph = workspace.workspace / "graphify-out" / "graph.json"
            result = await workspace.shell(
                shlex.join(
                    [
                        "graphify",
                        "query",
                        question,
                        "--budget",
                        str(budget),
                        "--graph",
                        str(graph),
                    ]
                )
            )
            if result.returncode:
                return f"graphify query failed: {result.stderr or result.stdout}"
            return result.stdout

        @function_tool
        def code_graph_search(query: str, kind: str | None = None, limit: int = 20) -> str:
            """Search symbols in the freshly generated code-review graph."""
            from code_review_graph.tools import semantic_search_nodes

            result = semantic_search_nodes(
                query=query,
                kind=kind,
                limit=limit,
                repo_root=str(workspace.workspace),
            )
            return json.dumps(result, default=str)

        @function_tool
        def code_graph_query(pattern: str, target: str) -> str:
            """Query callers, callees, imports, tests, inheritance, or file summaries."""
            from code_review_graph.tools import query_graph

            return json.dumps(
                query_graph(pattern=pattern, target=target, repo_root=str(workspace.workspace)),
                default=str,
            )

        @function_tool
        def code_graph_impact(changed_files: list[str] | None = None, max_depth: int = 2) -> str:
            """Check the code-review graph for the blast radius of proposed changes."""
            from code_review_graph.tools import get_impact_radius

            return json.dumps(
                get_impact_radius(
                    changed_files=changed_files,
                    max_depth=max_depth,
                    repo_root=str(workspace.workspace),
                ),
                default=str,
            )

        local_tools = [
            shell,
            read_file,
            write_file,
            replace_in_file,
            rg_conversation_history,
            graphify_query,
            code_graph_search,
            code_graph_query,
            code_graph_impact,
        ]
        mcp_servers = [
            item
            for item in connector_tools
            if all(hasattr(item, attribute) for attribute in ("list_tools", "call_tool", "connect"))
        ]
        sdk_tools = local_tools + [item for item in connector_tools if item not in mcp_servers]
        reasoning = (
            Reasoning(effort=self.config.model.reasoning)
            if self.config.model.reasoning is not None
            else None
        )
        model_settings = ModelSettings(
            temperature=self.config.model.temperature,
            top_p=self.config.model.top_p,
            max_tokens=self.config.model.max_tokens,
            # Every incident operation must gather evidence through at least one
            # available tool before it can report a result. Agent.reset_tool_choice
            # returns subsequent turns to automatic selection after the first call.
            tool_choice="required",
            parallel_tool_calls=self.config.model.parallel_tool_calls,
            reasoning=reasoning,
        )
        agent = Agent(
            name="Incident Resolver",
            instructions=instructions
            + "\n\nReturn only one JSON object matching the fields requested by the operation.",
            model=self._model(agents_module) if agents_module else self.config.model.name,
            model_settings=model_settings,
            tools=sdk_tools,
            mcp_servers=mcp_servers,
            reset_tool_choice=True,
        )
        result = await Runner.run(
            agent,
            prompt,
            max_turns=self.config.model.max_turns_per_iteration,
        )
        output = result.final_output
        if isinstance(output, dict):
            return output
        if not isinstance(output, str):
            raise RuntimeError("model returned a non-JSON result")
        try:
            decoded = json.loads(output)
        except json.JSONDecodeError as error:
            raise RuntimeError("model returned invalid JSON") from error
        if not isinstance(decoded, dict):
            raise RuntimeError("model JSON result must be an object")
        return decoded


class IncidentAgent:
    """One agent and one session, exposed through lifecycle-specific entry points."""

    def __init__(
        self,
        config: Config,
        storage: Storage,
        connectors: Any,
        backend: AgentBackend | None = None,
        skills_root: Path | str = "skills",
    ) -> None:
        self.config = config
        self.storage = storage
        self.connectors = connectors
        self.backend = backend
        self.skills_root = Path(skills_root)

    def _instructions(self, task: TaskRecord, worktree: Path, skills: list[str]) -> str:
        parts = [
            "# System Prompt\n\n" + self.config.agent.system_prompt.strip(),
            self.storage.read_memory(),
            self.storage.read_memory(task.repository),
        ]
        safety = self.config.safety
        safety_sections = {
            "Positive goals": safety.positive_goals,
            "Negative goals": safety.negative_goals,
            "Guardrails": safety.guardrails,
            "Safeguards": safety.safeguards,
        }
        configured_safety = [
            f"## {title}\n" + ("\n".join(f"- {item}" for item in values) or "- None configured")
            for title, values in safety_sections.items()
        ]
        parts.append(
            "# Binding Safety Contract\n\n"
            "Treat the following configured goals, guardrails, and safeguards as binding "
            "instructions for this run. If a requested action conflicts with them, stop and "
            "report the conflict instead of taking the action.\n\n" + "\n\n".join(configured_safety)
        )
        parts.append(
            "# Required Repository Graph Check\n\n"
            "This worktree was created only after pulling the latest configured base branch. "
            "Both `graphify` and `code-review-graph` were then generated from this exact checkout. "
            "Begin each operation by using `graphify_query` or `code_graph_search` to locate the "
            "relevant code before broad text search. Use `code_graph_query` to verify callers, "
            "callees, imports, tests, and inheritance. After changing code, use "
            "`code_graph_impact` to check blast radius. Treat graph results as navigation evidence "
            "and confirm conclusions against source and tests."
        )
        parts.append(
            "# Durable Conversation Recall\n\n"
            "Earlier incident messages are persisted but are not automatically included in this "
            "operation. If asked what was previously investigated, changed, tested, or why a "
            "decision was made and the current context is insufficient, call "
            "`rg_conversation_history` with a focused ripgrep regular expression. It searches only "
            "this incident and loads bounded matches into the current context. Treat those matches "
            "as evidence, and say that the answer is unknown when the persisted record is "
            "insufficient."
        )
        for skill in skills:
            path = self.skills_root / skill / "SKILL.md"
            if path.exists():
                parts.append(path.read_text(encoding="utf-8"))
        try:
            repository = self.config.repository(task.repository)
            project_instructions = worktree / repository.project_instructions
            if project_instructions.is_file():
                parts.append(project_instructions.read_text(encoding="utf-8"))
        except KeyError:
            pass
        return "\n\n".join(part for part in parts if part)

    @staticmethod
    async def _graph_context(task: TaskRecord, worktree: Path, tools: WorkspaceTools) -> str:
        """Query both fresh indexes before the model starts inspecting the checkout."""
        sections: list[str] = []
        graph = worktree / "graphify-out" / "graph.json"
        if graph.is_file():
            result = await tools.shell(
                shlex.join(
                    [
                        "graphify",
                        "query",
                        task.summary,
                        "--budget",
                        "2000",
                        "--graph",
                        "graphify-out/graph.json",
                    ]
                )
            )
            sections.append(
                "## graphify\n" + (result.stdout if result.returncode == 0 else result.stderr)
            )
        code_graph = worktree / ".code-review-graph" / "graph.db"
        if code_graph.is_file():
            from code_review_graph.tools import semantic_search_nodes

            result = await asyncio.to_thread(
                semantic_search_nodes,
                query=task.summary,
                limit=20,
                repo_root=str(worktree),
            )
            sections.append("## code-review-graph\n" + json.dumps(result, default=str))
        if not sections:
            return ""
        return (
            "# Fresh Repository Graph Context\n\n"
            + "\n\n".join(sections)
            + "\n\nConfirm graph leads against source and tests.\n\n"
        )

    async def _run(
        self,
        task: TaskRecord,
        worktree: Path,
        operation: str,
        prompt: str,
        skills: list[str],
        capabilities: set[str],
    ) -> dict[str, Any]:
        if not self.backend:
            raise RuntimeError("model backend is not configured")
        instructions = self._instructions(task, worktree, skills)
        tools = WorkspaceTools(
            worktree,
            timeout=self.config.model.tool_timeout_seconds,
            permissions=self.config.permissions,
            logger=lambda data: self.storage.append_event(
                task.task_id,
                TaskEvent(type=str(data.pop("type")), data=data),
            ),
            conversation_searcher=lambda pattern, limit: self.storage.search_messages(
                task.conversation_id, pattern, limit
            ),
        )
        connector_tools = await self.connectors.tools_for(capabilities)
        prompt = await self._graph_context(task, worktree, tools) + prompt
        self.storage.add_message(task.conversation_id, "user", f"{operation}: {prompt}")
        result = await self.backend(instructions, prompt, tools, connector_tools)
        self.storage.add_message(task.conversation_id, "assistant", str(result))
        return result

    async def investigate(self, task: TaskRecord, worktree: Path) -> InvestigationResult:
        incident = self.storage.load_incident(task.task_id)
        result = await self._run(
            task,
            worktree,
            "investigate",
            incident.model_dump_json(indent=2),
            ["graphify", "incident-investigation"],
            {"incidents", "errors", "logs", "traces", "metrics"},
        )
        return InvestigationResult.model_validate(result)

    async def implement_fix(self, task: TaskRecord, worktree: Path) -> FixResult:
        investigation = self.storage.task_directory(task.task_id) / "investigation.md"
        result = await self._run(
            task,
            worktree,
            "implement_fix",
            investigation.read_text() if investigation.exists() else task.summary,
            ["graphify", "coding", "testing", "github"],
            {"logs", "runtime"},
        )
        return FixResult.model_validate(result)

    async def address_review(
        self, task: TaskRecord, comments: list[ReviewComment], worktree: Path
    ) -> ReviewResult:
        result = await self._run(
            task,
            worktree,
            "address_review",
            "\n".join(f"{comment.author}: {comment.body}" for comment in comments),
            ["graphify", "review-comments", "coding", "testing"],
            set(),
        )
        return ReviewResult.model_validate(result)
