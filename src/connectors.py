"""Lifecycle and incident normalisation for external connectors."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .config import ConnectorConfig
from .models import Incident, IncidentEvidence


@dataclass(frozen=True)
class ConnectorTestResult:
    name: str
    connected: bool
    message: str = ""


class ConnectorManager:
    """Lifecycle manager for configured MCP servers and injected connector adapters."""

    def __init__(
        self,
        configs: list[ConnectorConfig],
        factories: dict[str, Callable[[ConnectorConfig], Awaitable[Any]]] | None = None,
    ) -> None:
        self.configs = {config.name: config for config in configs}
        self.factories = {
            config.name: self._mcp_factory for config in configs if config.type == "mcp"
        }
        self.factories.update(factories or {})
        self.sessions: dict[str, Any] = {}
        self.errors: dict[str, str] = {}

    @staticmethod
    async def _mcp_factory(config: ConnectorConfig) -> Any:
        from agents.mcp import MCPServerSse, MCPServerStdio, MCPServerStreamableHttp

        if config.transport == "stdio":
            server = MCPServerStdio(
                {
                    "command": config.command[0],
                    "args": config.command[1:],
                },
                name=config.name,
                cache_tools_list=True,
            )
        else:
            headers: dict[str, str] = {}
            if config.auth_token_env:
                token = os.getenv(config.auth_token_env)
                if not token:
                    raise RuntimeError(
                        f"environment variable {config.auth_token_env} is not configured"
                    )
                headers["Authorization"] = f"Bearer {token}"
            params = {"url": config.url or "", "headers": headers}
            server_type = MCPServerSse if config.transport == "sse" else MCPServerStreamableHttp
            server = server_type(params, name=config.name, cache_tools_list=True)
        await server.connect()
        return server

    async def start(self) -> None:
        for name, config in self.configs.items():
            if factory := self.factories.get(name):
                try:
                    self.sessions[name] = await factory(config)
                    self.errors.pop(name, None)
                except Exception as error:  # one unavailable connector must not stop intake
                    self.errors[name] = str(error)

    @staticmethod
    async def _close(session: Any) -> None:
        close = (
            getattr(session, "cleanup", None)
            or getattr(session, "aclose", None)
            or getattr(session, "close", None)
        )
        if close:
            result = close()
            if hasattr(result, "__await__"):
                await result

    async def stop(self) -> None:
        for session in self.sessions.values():
            await self._close(session)
        self.sessions.clear()

    async def tools_for(self, capabilities: set[str]) -> list[Any]:
        tools: list[Any] = []
        for name, session in self.sessions.items():
            if capabilities.intersection(self.configs[name].capabilities):
                if all(
                    hasattr(session, attribute)
                    for attribute in ("list_tools", "call_tool", "connect")
                ):
                    tools.append(session)
                    continue
                session_tools = getattr(session, "tools", [])
                if callable(session_tools):
                    session_tools = await session_tools()
                tools.extend(session_tools)
        return tools

    async def test_connection(self, name: str) -> ConnectorTestResult:
        if name not in self.configs:
            return ConnectorTestResult(name, False, "unknown connector")
        if name in self.sessions:
            return ConnectorTestResult(name, True, "connected")
        if name in self.errors:
            return ConnectorTestResult(name, False, self.errors[name])
        if name not in self.factories:
            return ConnectorTestResult(name, False, "no runtime adapter configured")
        try:
            session = await self.factories[name](self.configs[name])
            await self._close(session)
            return ConnectorTestResult(name, True, "connected")
        except Exception as error:  # connector errors are returned, not allowed to kill the TUI
            return ConnectorTestResult(name, False, str(error))

    def normalize_incident(self, name: str, payload: dict[str, Any]) -> Incident:
        if name not in self.configs:
            raise KeyError(f"connector is not configured: {name}")
        required = ("external_id", "repository", "environment", "summary")
        missing = [key for key in required if not payload.get(key)]
        if missing:
            raise ValueError(f"missing incident fields: {', '.join(missing)}")
        evidence = [IncidentEvidence.model_validate(item) for item in payload.get("evidence", [])]
        values: dict[str, Any] = dict(
            external_id=str(payload["external_id"]),
            source=name,
            repository=str(payload["repository"]),
            environment=str(payload["environment"]),
            summary=str(payload["summary"]),
            description=str(payload.get("description", "")),
            evidence=evidence,
        )
        if payload.get("received_at"):
            values["received_at"] = payload["received_at"]
        return Incident(**values)
