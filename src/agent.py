"""Bounded coding-agent facade with durable context assembly."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from .config import Config
from .models import (
    FixResult,
    InvestigationResult,
    ReviewComment,
    ReviewResult,
    SessionResult,
    TaskEvent,
    TaskRecord,
)
from .skills import Skill, SkillResolver
from .storage import Storage
from .tools import WorkspaceTools
from .tooling import subscription_cli_command

AgentBackend = Callable[[str, str, WorkspaceTools, list[Any]], Awaitable[dict[str, Any]]]


class AgentLifecycle(Protocol):
    """Task-state operations exposed to a long-running agent session."""

    async def mark_investigation_complete(
        self,
        root_cause: str,
        evidence: list[str],
        proposed_fix: str,
        reproducible: bool = False,
    ) -> dict[str, Any]: ...

    async def run_tests(self, command: str) -> dict[str, Any]: ...

    async def open_pr(self, summary: str) -> dict[str, Any]: ...

    async def remember(self, note: str, scope: str = "task") -> dict[str, Any]: ...


@dataclass(frozen=True)
class AgentRunContext:
    """Durable identity and callbacks shared by every resume of a task session."""

    task: TaskRecord
    session_id: str
    session_db: Path
    lifecycle: AgentLifecycle
    save_backend_session: Callable[[str], None]
    memory_writer: Callable[[str], None]
    capabilities: frozenset[str] = frozenset()
    connector_tools: tuple[Any, ...] = ()


class _CompactingSession:
    """Bound a SQLite SDK session while retaining an extractive task-memory checkpoint."""

    def __init__(
        self,
        session: Any,
        *,
        threshold: int,
        keep: int,
        memory_writer: Callable[[str], None],
    ) -> None:
        self.session_id = session.session_id
        self.session_settings = getattr(session, "session_settings", None)
        self._session = session
        self._threshold = threshold
        self._keep = keep
        self._memory_writer = memory_writer

    async def get_items(self, limit: int | None = None) -> list[Any]:
        return await self._session.get_items(limit)

    async def add_items(self, items: list[Any]) -> None:
        await self._session.add_items(items)
        await self._compact_if_needed()

    async def pop_item(self) -> Any | None:
        return await self._session.pop_item()

    async def clear_session(self) -> None:
        await self._session.clear_session()

    @staticmethod
    def _text(item: Any) -> str:
        if not isinstance(item, dict):
            return str(item)
        content = item.get("content", "")
        if isinstance(content, list):
            parts = [
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            ]
            content = " ".join(part for part in parts if part)
        return " ".join(str(content).split())

    async def _compact_if_needed(self) -> None:
        items = await self._session.get_items()
        if len(items) <= self._threshold:
            return
        old_items = items[: -self._keep]
        recent_items = items[-self._keep :]
        lines = [self._text(item)[:500] for item in old_items if self._text(item)]
        checkpoint = "\n".join(f"- {line}" for line in lines[-40:])
        summary = (
            f"## Compacted session {self.session_id}\n\n"
            f"{checkpoint or '- Earlier tool and model activity was compacted.'}\n"
        )
        self._memory_writer(summary)
        await self._session.clear_session()
        await self._session.add_items(
            [
                {
                    "role": "user",
                    "content": "Durable checkpoint from earlier session context:\n" + summary,
                },
                *recent_items,
            ]
        )


class _ConsoleProgress:
    """Render compact agent activity without leaking model or tool payloads."""

    _DETAIL_LIMIT = 160
    _REASONING_LIMIT = 2_000

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._reasoning_characters = 0
        self._reasoning_open = False
        self._reasoning_streamed = False
        self._active_tools: list[str] = []

    @staticmethod
    def _field(value: Any, name: str, default: Any = None) -> Any:
        if isinstance(value, dict):
            return value.get(name, default)
        return getattr(value, name, default)

    @classmethod
    def _compact(cls, value: Any, limit: int | None = None) -> str:
        text = " ".join(str(value or "").split())
        maximum = limit or cls._DETAIL_LIMIT
        return text if len(text) <= maximum else text[: maximum - 1].rstrip() + "…"

    @classmethod
    def _reasoning_text(cls, value: Any) -> str:
        """Extract provider reasoning summaries without opaque signatures."""
        parts = cls._field(value, "summary") or cls._field(value, "content") or []
        texts: list[str] = []
        for part in parts if isinstance(parts, list) else [parts]:
            text = cls._field(part, "text")
            if text:
                texts.append(str(text))
        return "\n".join(texts)

    @classmethod
    def _tool_call(cls, value: Any) -> tuple[str, str]:
        name = str(cls._field(value, "name", "tool") or "tool")
        arguments = cls._field(value, "arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        if not isinstance(arguments, dict):
            return name, ""

        preferred_keys = {
            "shell": ("command",),
            "read_file": ("path",),
            "write_file": ("path",),
            "replace_in_file": ("path",),
            "rg_conversation_history": ("pattern",),
            "code_graph_search": ("query",),
            "code_graph_query": ("target", "pattern"),
            "code_graph_impact": ("changed_files",),
        }
        keys = preferred_keys.get(name, ("path", "command", "query", "target"))
        detail = next((arguments[key] for key in keys if arguments.get(key) is not None), "")
        if isinstance(detail, list):
            detail = ", ".join(str(item) for item in detail)
        return name, cls._compact(detail)

    def _close_reasoning(self) -> None:
        if self.enabled and self._reasoning_open:
            print(flush=True)
        self._reasoning_open = False

    def _line(self, marker: str, message: str) -> None:
        if not self.enabled:
            return
        self._close_reasoning()
        print(f"{marker} {message}", flush=True)

    def _reasoning_delta(self, delta: str) -> None:
        if not self.enabled or self._reasoning_characters >= self._REASONING_LIMIT:
            return
        if not self._reasoning_open:
            print("  thinking  ", end="", flush=True)
            self._reasoning_open = True
        remaining = self._REASONING_LIMIT - self._reasoning_characters
        rendered = delta[:remaining]
        print(rendered, end="", flush=True)
        self._reasoning_characters += len(rendered)
        self._reasoning_streamed = True
        if len(delta) > remaining:
            print("…", end="", flush=True)

    def start(self, *, model: str, max_turns: int, tool_count: int) -> None:
        self._line(
            "•",
            f"Incident Resolver · {model} · {tool_count} tools · {max_turns} turns max",
        )

    def complete(self) -> None:
        self._line("✓", "Agent run completed")

    def fail(self, error: Exception) -> None:
        message = self._compact(str(error)) or type(error).__name__
        self._line("!", f"Agent run failed: {message}")

    def event(self, event: Any) -> None:
        """Render only operator-useful summaries from SDK stream events."""
        if not self.enabled:
            return
        event_type = str(self._field(event, "type", type(event).__name__))
        if event_type == "raw_response_event":
            data = self._field(event, "data", event)
            data_type = str(self._field(data, "type", type(data).__name__))
            delta = self._field(data, "delta")
            if "reasoning_summary" in data_type and isinstance(delta, str):
                self._reasoning_delta(delta)
            # Final response deltas are structured workflow data, not terminal prose.
            return

        if event_type == "run_item_stream_event":
            name = str(self._field(event, "name", "run item"))
            item = self._field(event, "item")
            raw_item = self._field(item, "raw_item", item)
            if name == "reasoning_item_created":
                reasoning = self._reasoning_text(raw_item)
                if reasoning and not self._reasoning_streamed:
                    self._line("•", f"Thinking: {self._compact(reasoning)}")
            elif name == "tool_called":
                tool_name, detail = self._tool_call(raw_item)
                self._active_tools.append(tool_name)
                self._line("→", f"{tool_name}: {detail}" if detail else tool_name)
            elif name == "tool_output":
                tool_name = self._active_tools.pop(0) if self._active_tools else "Tool"
                output = self._field(item, "output", "")
                try:
                    result = json.loads(output) if isinstance(output, str) else output
                except json.JSONDecodeError:
                    result = None
                returncode = self._field(result, "returncode") if result is not None else None
                if isinstance(returncode, int) and returncode != 0:
                    self._line("!", f"{tool_name} failed (exit {returncode})")
                else:
                    self._line("✓", f"{tool_name} completed")
            elif name == "handoff_requested":
                target = self._field(raw_item, "target") or self._field(raw_item, "name")
                suffix = f": {self._compact(target)}" if target else ""
                self._line("→", f"Handoff requested{suffix}")
            return

        if event_type == "agent_updated_stream_event":
            agent = self._field(event, "new_agent")
            name = self._field(agent, "name", "agent")
            self._line("→", f"Agent: {self._compact(name)}")


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
        output_type: type[BaseModel] | None = None,
        run_context: AgentRunContext | None = None,
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

        lifecycle_tools: list[Any] = []
        if run_context is not None:

            @function_tool
            async def mark_investigation_complete(
                root_cause: str,
                evidence: list[str],
                proposed_fix: str,
                reproducible: bool = False,
            ) -> str:
                """Persist the evidence-backed investigation before implementation starts."""
                return json.dumps(
                    await run_context.lifecycle.mark_investigation_complete(
                        root_cause, evidence, proposed_fix, reproducible
                    )
                )

            @function_tool
            async def run_tests(command: str) -> str:
                """Run a verification command and durably record its result and state."""
                return json.dumps(await run_context.lifecycle.run_tests(command))

            @function_tool
            async def open_pr(summary: str) -> str:
                """Publish or update the verified fix and yield for deployment or review."""
                return json.dumps(await run_context.lifecycle.open_pr(summary))

            @function_tool
            async def remember(note: str, scope: str = "task") -> str:
                """Store a durable task or repository memory for later resumes."""
                return json.dumps(await run_context.lifecycle.remember(note, scope))

            lifecycle_tools = [
                mark_investigation_complete,
                run_tests,
                open_pr,
                remember,
            ]

        local_tools: list[Any] = [
            shell,
            read_file,
            write_file,
            replace_in_file,
            rg_conversation_history,
            code_graph_search,
            code_graph_query,
            code_graph_impact,
        ]
        mcp_servers = [
            item
            for item in connector_tools
            if all(hasattr(item, attribute) for attribute in ("list_tools", "call_tool", "connect"))
        ]
        sdk_tools = (
            local_tools
            + lifecycle_tools
            + [item for item in connector_tools if item not in mcp_servers]
        )
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
        model = self._model(agents_module) if agents_module else self.config.model.name
        session: Any | None = None
        if run_context is not None:
            from agents import SQLiteSession

            def durable_session(session_id: str) -> Any:
                sqlite_session = SQLiteSession(session_id, db_path=run_context.session_db)
                return (
                    _CompactingSession(
                        sqlite_session,
                        threshold=self.config.model.compaction_threshold,
                        keep=self.config.model.session_history_limit,
                        memory_writer=lambda value: self._write_compaction_memory(
                            run_context, value
                        ),
                    )
                    if self.config.model.compaction_enabled
                    else sqlite_session
                )

            session = durable_session(run_context.session_id)

        subagent_tools: list[Any] = []
        if run_context is not None and self.config.agent.max_subagents:
            read_tools: list[Any] = [
                shell,
                read_file,
                rg_conversation_history,
                code_graph_search,
                code_graph_query,
                code_graph_impact,
            ]
            research = Agent(
                name="Incident Researcher",
                instructions=(
                    instructions
                    + "\n\nInvestigate the delegated sub-task. Do not modify files. Return concise "
                    "evidence, hypotheses, and source/test locations to the parent agent."
                ),
                model=model,
                model_settings=model_settings,
                tools=read_tools,
                mcp_servers=mcp_servers,
                reset_tool_choice=True,
            )
            implementer = Agent(
                name="Incident Implementer",
                instructions=(
                    instructions
                    + "\n\nImplement the delegated, bounded change and regression test. Report "
                    "changed files and commands; the parent owns lifecycle transitions."
                ),
                model=model,
                model_settings=model_settings,
                tools=local_tools,
                mcp_servers=mcp_servers,
                reset_tool_choice=True,
            )
            research_session = durable_session(f"{run_context.session_id}:research")
            implementation_session = durable_session(f"{run_context.session_id}:implementation")
            subagent_tools = [
                research.as_tool(
                    "delegate_research",
                    "Delegate a bounded code or incident research sub-task.",
                    max_turns=self.config.model.max_turns_per_iteration,
                    session=research_session,
                ),
                implementer.as_tool(
                    "delegate_implementation",
                    "Delegate a bounded implementation or test sub-task.",
                    max_turns=self.config.model.max_turns_per_iteration,
                    session=implementation_session,
                ),
            ][: self.config.agent.max_subagents]

        agent = Agent(
            name="Incident Resolver",
            instructions=instructions
            + "\n\nReturn only the structured checkpoint requested after lifecycle tools have "
            "persisted the task's real progress.",
            model=model,
            model_settings=model_settings,
            tools=sdk_tools + subagent_tools,
            mcp_servers=mcp_servers,
            output_type=output_type,
            reset_tool_choice=True,
        )
        progress = _ConsoleProgress(self.config.model.show_execution_details)
        progress.start(
            model=self.config.model.name,
            max_turns=self.config.model.max_turns_per_iteration,
            tool_count=len(sdk_tools) + len(mcp_servers),
        )
        stream_runner = getattr(Runner, "run_streamed", None)
        run_kwargs: dict[str, Any] = {
            "max_turns": self.config.model.max_turns_per_iteration,
        }
        if session is not None:
            run_kwargs["session"] = session
        try:
            if self.config.model.show_execution_details and callable(stream_runner):
                result = stream_runner(
                    agent,
                    prompt,
                    **run_kwargs,
                )
                try:
                    async for event in result.stream_events():
                        progress.event(event)
                finally:
                    progress._close_reasoning()  # noqa: SLF001
            else:
                result = await Runner.run(
                    agent,
                    prompt,
                    **run_kwargs,
                )
            output = result.final_output
            if isinstance(output, BaseModel):
                decoded = output.model_dump(mode="json")
            elif isinstance(output, dict):
                decoded = output
            elif not isinstance(output, str):
                raise RuntimeError("model returned a non-JSON result")
            else:
                try:
                    decoded = json.loads(output)
                except json.JSONDecodeError as error:
                    raise RuntimeError("model returned invalid JSON") from error
                if not isinstance(decoded, dict):
                    raise RuntimeError("model JSON result must be an object")
        except Exception as error:
            progress.fail(error)
            raise
        progress.complete()
        return decoded

    @staticmethod
    def _write_compaction_memory(run_context: AgentRunContext, value: str) -> None:
        run_context.memory_writer(value)


class SubscriptionCLIBackend:
    """Long-session backend using a host-authenticated subscription CLI (Codex by default)."""

    _TOOL_HELP = """
# Harness lifecycle tools

Use the following command from the repository root to drive durable task state. Pass exactly one
JSON object as the final argument and use the returned JSON as authoritative:

- `harness-out/incident-session-tool mark_investigation_complete JSON`
  (`root_cause`, `evidence`, `proposed_fix`, `reproducible`)
- `harness-out/incident-session-tool run_tests JSON` (`command`)
- `harness-out/incident-session-tool open_pr JSON` (`summary`)
- `harness-out/incident-session-tool remember JSON` (`note`, optional `scope`)
- `harness-out/incident-session-tool shell JSON` (`command`)
- `harness-out/incident-session-tool read_file JSON` (`path`)
- `harness-out/incident-session-tool write_file JSON` (`path`, `content`)
- `harness-out/incident-session-tool replace_in_file JSON` (`path`, `old`, `new`)
- `harness-out/incident-session-tool code_graph_search JSON` (`query`, optional `kind`, `limit`)
- `harness-out/incident-session-tool code_graph_query JSON` (`pattern`, `target`)
- `harness-out/incident-session-tool code_graph_impact JSON` (`changed_files`, optional `max_depth`)
- `harness-out/incident-session-tool connector_call JSON` (`connector`, `tool`, `arguments`)

The CLI runs in a read-only sandbox. Use these mapped commands for mutations and verification so
the harness applies its workspace and permission policy. Native read-only inspection remains
available. Configured MCP servers are supplied to the CLI process. Do not claim a transition unless
its lifecycle command succeeds.
"""

    def __init__(self, config: Config) -> None:
        self.config = config

    @staticmethod
    def _connector_name(server: Any, index: int) -> str:
        return str(getattr(server, "name", None) or f"connector-{index}")

    async def _connector_help(self, context: AgentRunContext) -> str:
        lines: list[str] = []
        for index, server in enumerate(context.connector_tools):
            if not all(
                callable(getattr(server, attribute, None))
                for attribute in ("list_tools", "call_tool")
            ):
                continue
            name = self._connector_name(server, index)
            try:
                tools = await server.list_tools()
            except Exception as error:
                lines.append(f"- {name}: unavailable ({error})")
                continue
            tool_names: list[str] = []
            for tool in tools:
                name_value = (
                    tool.get("name", "tool")
                    if isinstance(tool, dict)
                    else getattr(tool, "name", "tool")
                )
                tool_names.append(str(name_value))
            lines.append(f"- {name}: {', '.join(tool_names) or 'no tools reported'}")
        return "\n".join(lines) or "- No runtime MCP adapters are connected."

    @staticmethod
    def _launcher(path: Path, socket_path: Path, token: str) -> None:
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, socket, sys\n"
            f"request={{'token':{token!r},'name':sys.argv[1],"
            "'arguments':json.loads(sys.argv[2])}\n"
            "client=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n"
            f"client.connect({str(socket_path)!r})\n"
            "client.sendall((json.dumps(request)+'\\n').encode())\n"
            "data=b''\n"
            "while not data.endswith(b'\\n'):\n"
            "    chunk=client.recv(65536)\n"
            "    if not chunk: break\n"
            "    data+=chunk\n"
            "client.close()\n"
            "print(data.decode().strip())\n",
            encoding="utf-8",
        )
        path.chmod(0o700)

    @asynccontextmanager
    async def _tool_bridge(self, context: AgentRunContext, workspace: WorkspaceTools):
        digest = hashlib.sha256(context.session_id.encode()).hexdigest()[:16]
        socket_path = Path(tempfile.gettempdir()) / f"ih-{digest}.sock"
        with suppress(FileNotFoundError):
            socket_path.unlink()
        token = secrets.token_hex(24)

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                request = json.loads((await reader.readline()).decode())
                if not secrets.compare_digest(str(request.get("token", "")), token):
                    raise PermissionError("invalid session-tool token")
                name = str(request.get("name", ""))
                if name not in {
                    "mark_investigation_complete",
                    "run_tests",
                    "open_pr",
                    "remember",
                    "shell",
                    "read_file",
                    "write_file",
                    "replace_in_file",
                    "code_graph_search",
                    "code_graph_query",
                    "code_graph_impact",
                    "connector_call",
                }:
                    raise ValueError(f"unknown lifecycle tool: {name}")
                arguments = request.get("arguments", {})
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments must be an object")
                if name in {
                    "mark_investigation_complete",
                    "run_tests",
                    "open_pr",
                    "remember",
                }:
                    result = await getattr(context.lifecycle, name)(**arguments)
                elif name == "shell":
                    command_result = await workspace.shell(**arguments)
                    result = {
                        "returncode": command_result.returncode,
                        "stdout": command_result.stdout,
                        "stderr": command_result.stderr,
                        "truncated": command_result.truncated,
                    }
                elif name == "read_file":
                    result = {"content": workspace.read_file(arguments["path"])}
                elif name == "write_file":
                    workspace.write_file(arguments["path"], arguments["content"])
                    result = {"written": True}
                elif name == "replace_in_file":
                    workspace.replace_in_file(
                        arguments["path"], arguments["old"], arguments["new"]
                    )
                    result = {"replaced": True}
                elif name == "connector_call":
                    connector = str(arguments.get("connector", ""))
                    tool_name = str(arguments.get("tool", ""))
                    tool_arguments = arguments.get("arguments", {})
                    if not isinstance(tool_arguments, dict):
                        raise ValueError("connector arguments must be an object")
                    server = next(
                        (
                            server
                            for index, server in enumerate(context.connector_tools)
                            if self._connector_name(server, index) == connector
                        ),
                        None,
                    )
                    if server is None:
                        raise ValueError(f"unknown connector: {connector}")
                    result = await server.call_tool(tool_name, tool_arguments)
                elif name == "code_graph_search":
                    from code_review_graph.tools import semantic_search_nodes

                    result = semantic_search_nodes(
                        query=str(arguments["query"]),
                        kind=arguments.get("kind"),
                        limit=int(arguments.get("limit", 20)),
                        repo_root=str(workspace.workspace),
                    )
                elif name == "code_graph_query":
                    from code_review_graph.tools import query_graph

                    result = query_graph(
                        pattern=str(arguments["pattern"]),
                        target=str(arguments["target"]),
                        repo_root=str(workspace.workspace),
                    )
                else:
                    from code_review_graph.tools import get_impact_radius

                    result = get_impact_radius(
                        changed_files=arguments.get("changed_files"),
                        max_depth=int(arguments.get("max_depth", 2)),
                        repo_root=str(workspace.workspace),
                    )
                response = {"ok": True, "result": result}
            except Exception as error:
                response = {"ok": False, "error": str(error)}
            writer.write((json.dumps(response, default=str) + "\n").encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(handle, path=socket_path)
        launcher = workspace.workspace / "harness-out" / "incident-session-tool"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        self._launcher(launcher, socket_path, token)
        try:
            async with server:
                yield launcher
        finally:
            server.close()
            await server.wait_closed()
            with suppress(FileNotFoundError):
                socket_path.unlink()
            with suppress(FileNotFoundError):
                launcher.unlink()

    def _mcp_arguments(self, capabilities: frozenset[str]) -> list[str]:
        arguments: list[str] = []
        for connector in self.config.connectors:
            if connector.type != "mcp" or not capabilities.intersection(connector.capabilities):
                continue
            prefix = f"mcp_servers.{connector.name}"
            if connector.transport == "stdio":
                arguments.extend(["-c", f"{prefix}.command={json.dumps(connector.command[0])}"])
                if len(connector.command) > 1:
                    arguments.extend(
                        ["-c", f"{prefix}.args={json.dumps(connector.command[1:])}"]
                    )
            else:
                arguments.extend(["-c", f"{prefix}.url={json.dumps(connector.url)}"])
                if connector.auth_token_env:
                    arguments.extend(
                        [
                            "-c",
                            f"{prefix}.bearer_token_env_var="
                            f"{json.dumps(connector.auth_token_env)}",
                        ]
                    )
        return arguments

    def _subscription_command(self) -> list[str]:
        """Return the configured CLI command with Codex's yolo mode enabled."""
        return subscription_cli_command(list(self.config.model.subscription_command))

    @staticmethod
    def _decode_output(stdout: str, output_path: Path) -> tuple[str | None, dict[str, Any]]:
        backend_session: str | None = None
        messages: list[str] = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started":
                backend_session = str(event.get("thread_id") or event.get("thread", {}).get("id"))
            item = event.get("item", {})
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                messages.append(str(item.get("text", "")))
        raw = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        raw = raw.strip() or (messages[-1].strip() if messages else "")
        if not raw:
            raise RuntimeError("subscription CLI returned no final structured result")
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError("subscription CLI returned invalid JSON") from error
        if not isinstance(decoded, dict):
            raise RuntimeError("subscription CLI result must be an object")
        return backend_session, decoded

    async def __call__(
        self,
        instructions: str,
        prompt: str,
        workspace: WorkspaceTools,
        connector_tools: list[Any],
        output_type: type[BaseModel] | None = None,
        run_context: AgentRunContext | None = None,
    ) -> dict[str, Any]:
        del connector_tools  # Configured MCP endpoints are translated into CLI arguments below.
        if run_context is None:
            raise RuntimeError("subscription CLI requires a durable task run context")
        output_type = output_type or SessionResult
        files = workspace.workspace / "harness-out"
        files.mkdir(parents=True, exist_ok=True)
        schema_path = files / "session-output.schema.json"
        output_path = files / "session-output.json"
        schema_path.write_text(json.dumps(output_type.model_json_schema()), encoding="utf-8")
        with suppress(FileNotFoundError):
            output_path.unlink()

        command = [*self._subscription_command(), "exec"]
        backend_session = run_context.task.backend_session_id
        if backend_session:
            command.extend(["resume", backend_session])
        else:
            command.extend(["--sandbox", "read-only", "--cd", str(workspace.workspace)])
        command.extend(self._mcp_arguments(run_context.capabilities))
        if self.config.model.subscription_profile:
            command.extend(["--profile", self.config.model.subscription_profile])
        command.extend(
            [
                "--json",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "-",
            ]
        )
        connector_help = await self._connector_help(run_context)
        full_prompt = (
            instructions
            + "\n\n"
            + self._TOOL_HELP
            + "\n\nAvailable runtime MCP adapters:\n"
            + connector_help
            + "\n\n"
            + prompt
        )
        progress = _ConsoleProgress(self.config.model.show_execution_details)
        progress.start(
            model="subscription-cli",
            max_turns=self.config.model.max_turns_per_iteration,
            tool_count=4,
        )
        try:
            async with self._tool_bridge(run_context, workspace):
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=workspace.workspace,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        process.communicate(full_prompt.encode()),
                        timeout=self.config.model.tool_timeout_seconds,
                    )
                except TimeoutError as error:
                    process.kill()
                    await process.wait()
                    raise RuntimeError(
                        "subscription CLI timed out after "
                        f"{self.config.model.tool_timeout_seconds}s"
                    ) from error
            stdout = stdout_bytes.decode(errors="replace")
            stderr = stderr_bytes.decode(errors="replace")
            if process.returncode:
                raise RuntimeError(
                    f"subscription CLI exited {process.returncode}: "
                    + (stderr or stdout)[-4000:]
                )
            discovered_session, decoded = self._decode_output(stdout, output_path)
            if discovered_session and discovered_session != "None":
                run_context.save_backend_session(discovered_session)
            validated = output_type.model_validate(decoded)
            progress.complete()
            return validated.model_dump(mode="json")
        except Exception as error:
            progress.fail(error)
            raise
        finally:
            for path in (schema_path, output_path):
                with suppress(FileNotFoundError):
                    path.unlink()


class IncidentAgent:
    """One agent and one session, exposed through lifecycle-specific entry points."""

    def __init__(
        self,
        config: Config,
        storage: Storage,
        connectors: Any,
        backend: AgentBackend | None = None,
        skills_root: Path | str | None = None,
    ) -> None:
        self.config = config
        self.storage = storage
        self.connectors = connectors
        self.backend = backend
        packaged_skills = Path(__file__).with_name("builtin_skills")
        source_skills = Path(__file__).resolve().parent.parent / "skills"
        self.skills_root = (
            Path(skills_root)
            if skills_root is not None
            else packaged_skills
            if packaged_skills.is_dir()
            else source_skills
        )

    @property
    def supports_durable_session(self) -> bool:
        return isinstance(self.backend, (OpenAIAgentsBackend, SubscriptionCLIBackend))

    def _instructions(self, task: TaskRecord, worktree: Path, skills: tuple[Skill, ...]) -> str:
        parts = [
            "# System Prompt\n\n" + self.config.agent.system_prompt.strip(),
            self.storage.read_memory(),
            self.storage.read_memory(task.repository),
            self.storage.read_task_memory(task.task_id),
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
            "A fresh `code-review-graph` index was generated from this exact checkout. "
            "Begin each operation with `code_graph_search` to locate relevant code before broad "
            "text search. Use `code_graph_query` to verify callers, callees, imports, tests, and "
            "inheritance. After changing code, use `code_graph_impact` to check blast radius. "
            "Treat graph results as navigation evidence and confirm conclusions against source "
            "and tests."
        )
        parts.append(
            "# Durable Conversation Recall\n\n"
            "This task has one durable agent session, including stable research and implementation "
            "sub-agent sessions. The runtime automatically restores recent context and compacts "
            "older context into task memory. If the restored context is insufficient, call "
            "`rg_conversation_history` with a focused ripgrep regular expression. It searches only "
            "this incident and loads bounded matches into the current context. Treat those matches "
            "as evidence, and say that the answer is unknown when the persisted record is "
            "insufficient."
        )
        parts.append(
            "# Agent-Driven Lifecycle\n\n"
            "You own the incident lifecycle. Delegate bounded research and implementation work to "
            "the available sub-agents while remaining responsible for their conclusions. Persist "
            "the root cause with `mark_investigation_complete`; use `run_tests` for every real "
            "verification command; and call `open_pr` only after the relevant checks pass. These "
            "tools, not prose or a final response, move durable task state. Use `remember` for "
            "decisions that must survive compaction. Yield once the task is waiting for an "
            "external deployment or review event, and resume the same session when the harness "
            "supplies it."
        )
        if skills:
            loaded = ", ".join(skill.name for skill in skills)
            parts.append(
                "# Preflight Skill Resolution\n\n"
                f"The resolver searched the configured skill directories before this run and "
                f"loaded these applicable skills in order: {loaded}. Follow them for this "
                "operation; their full instructions appear below."
            )
            parts.extend(skill.content for skill in skills)
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
        """Query the fresh code-review graph before the model starts inspecting the checkout."""
        code_graph = worktree / ".code-review-graph" / "graph.db"
        if not code_graph.is_file():
            return ""
        from code_review_graph.tools import semantic_search_nodes

        result = await asyncio.to_thread(
            semantic_search_nodes,
            query=task.summary,
            limit=20,
            repo_root=str(worktree),
        )
        return (
            "# Fresh Repository Graph Context\n\n"
            "## code-review-graph\n"
            + json.dumps(result, default=str)
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
        lifecycle: AgentLifecycle | None = None,
    ) -> dict[str, Any]:
        if not self.backend:
            raise RuntimeError("model backend is not configured")
        roots = [self.skills_root]
        roots.extend(worktree / directory for directory in self.config.agent.skill_directories)
        resolution = SkillResolver(
            roots, max_auto_skills=self.config.agent.max_auto_skills
        ).resolve(skills, f"{operation}\n{task.summary}\n{prompt}")
        self.storage.append_event(
            task.task_id,
            TaskEvent(
                type="agent.skills_resolved",
                data={
                    "discovered": len(resolution.discovered),
                    "loaded": [skill.name for skill in resolution.selected],
                    "missing_required": list(resolution.missing_required),
                },
            ),
        )
        if resolution.missing_required:
            missing = ", ".join(resolution.missing_required)
            raise RuntimeError(f"required agent skills were not found: {missing}")
        instructions = self._instructions(task, worktree, resolution.selected)
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
        if isinstance(self.backend, (OpenAIAgentsBackend, SubscriptionCLIBackend)):
            output_types: dict[str, type[BaseModel]] = {
                "investigate": InvestigationResult,
                "implement_fix": FixResult,
                "address_review": ReviewResult,
                "resolve": SessionResult,
            }
            run_context: AgentRunContext | None = None
            if lifecycle is not None:
                session_id = task.agent_session_id or task.conversation_id

                def save_backend_session(value: str) -> None:
                    latest = self.storage.load_task(task.task_id)
                    latest.backend_session_id = value
                    self.storage.save_task(latest)

                run_context = AgentRunContext(
                    task=task,
                    session_id=session_id,
                    session_db=self.storage.root / "sessions.sqlite3",
                    lifecycle=lifecycle,
                    save_backend_session=save_backend_session,
                    memory_writer=lambda value: self.storage.append_task_memory(
                        task.task_id, value
                    ),
                    capabilities=frozenset(capabilities),
                    connector_tools=tuple(connector_tools),
                )
            backend_kwargs: dict[str, Any] = {"output_type": output_types[operation]}
            if run_context is not None:
                backend_kwargs["run_context"] = run_context
            return await self.backend(
                instructions,
                prompt,
                tools,
                connector_tools,
                **backend_kwargs,
            )
        return await self.backend(instructions, prompt, tools, connector_tools)

    def _cache_response(self, task: TaskRecord, response: dict[str, Any]) -> None:
        """Persist only a model response that passed the operation schema."""
        self.storage.add_message(task.conversation_id, "assistant", str(response))

    async def run_session(
        self,
        task: TaskRecord,
        worktree: Path,
        lifecycle: AgentLifecycle,
        prompt: str | None = None,
    ) -> SessionResult:
        incident = self.storage.load_incident(task.task_id)
        try:
            repository = self.config.repository(task.repository)
            base_branch = repository.base_branch
        except KeyError:
            base_branch = "main"
        await asyncio.to_thread(self.storage.refresh_worktree, worktree, base_branch)
        initial_prompt = (
            "Resolve this incident end to end in the current durable session. Use lifecycle tools "
            "to persist progress and delegate bounded sub-tasks.\n\n"
            + incident.model_dump_json(indent=2)
        )
        result = await self._run(
            task,
            worktree,
            "resolve",
            prompt or initial_prompt,
            [
                "code-review-graph",
                "incident-investigation",
                "coding",
                "testing",
                "github",
            ],
            {"incidents", "errors", "logs", "traces", "metrics", "runtime"},
            lifecycle=lifecycle,
        )
        validated = SessionResult.model_validate(result)
        self._cache_response(task, result)
        return validated

    async def investigate(self, task: TaskRecord, worktree: Path) -> InvestigationResult:
        incident = self.storage.load_incident(task.task_id)
        result = await self._run(
            task,
            worktree,
            "investigate",
            incident.model_dump_json(indent=2),
            ["code-review-graph", "incident-investigation"],
            {"incidents", "errors", "logs", "traces", "metrics"},
        )
        validated = InvestigationResult.model_validate(result)
        self._cache_response(task, result)
        return validated

    async def implement_fix(self, task: TaskRecord, worktree: Path) -> FixResult:
        investigation = self.storage.task_directory(task.task_id) / "investigation.md"
        result = await self._run(
            task,
            worktree,
            "implement_fix",
            investigation.read_text() if investigation.exists() else task.summary,
            ["code-review-graph", "coding", "testing", "github"],
            {"logs", "runtime"},
        )
        validated = FixResult.model_validate(result)
        self._cache_response(task, result)
        return validated

    async def address_review(
        self, task: TaskRecord, comments: list[ReviewComment], worktree: Path
    ) -> ReviewResult:
        result = await self._run(
            task,
            worktree,
            "address_review",
            "\n".join(f"{comment.author}: {comment.body}" for comment in comments),
            ["code-review-graph", "review-comments", "coding", "testing"],
            set(),
        )
        validated = ReviewResult.model_validate(result)
        self._cache_response(task, result)
        return validated
