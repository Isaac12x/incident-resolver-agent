"""Read-only, tenant-aware Loki/Grafana tools shared by both agent runtimes."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from urllib.parse import quote

import httpx
from agents.mcp import MCPServer
from mcp.types import CallToolResult, GetPromptResult, ListPromptsResult, TextContent, Tool
from pydantic import BaseModel, ConfigDict, Field

from .config import ConnectorConfig


class LogQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=4000)
    start: str = Field(description="Incident window start: RFC3339 UTC or epoch nanoseconds")
    end: str = Field(description="Incident window end: RFC3339 UTC or epoch nanoseconds")
    limit: int = Field(default=100, ge=1, le=200)


class ObservabilityServer(MCPServer):
    def __init__(self, config: ConnectorConfig) -> None:
        super().__init__()
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    async def connect(self) -> None:
        # Requests use short-lived clients; an outage must not hide tools on recovery.
        pass

    async def cleanup(self) -> None:
        pass

    async def list_prompts(self) -> ListPromptsResult:
        return ListPromptsResult(prompts=[])

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> GetPromptResult:
        raise ValueError("observability connectors do not provide prompts")

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.config.auth_token_env:
            token = os.getenv(self.config.auth_token_env)
            if not token:
                raise ValueError(f"missing environment variable {self.config.auth_token_env}")
            headers["Authorization"] = f"Bearer {token}"
        if self.config.type == "loki" and self.config.tenant_id:
            headers["X-Scope-OrgID"] = self.config.tenant_id
        return headers

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        # Never return upstream error bodies/URLs: they may contain credentials or log data.
        try:
            async with (
                asyncio.timeout(20),
                httpx.AsyncClient(
                    timeout=10, follow_redirects=False, headers=self._headers()
                ) as client,
                client.stream(
                    "GET", (self.config.url or "").rstrip("/") + path, params=params
                ) as response,
            ):
                if response.status_code != 200:
                    raise RuntimeError(f"{self.name}: HTTP {response.status_code}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 1_000_000:
                        raise ValueError("response exceeds 1 MB; narrow the query window")
                return json.loads(body)
        except (httpx.HTTPError, TimeoutError) as error:
            raise RuntimeError(f"{self.name}: connection failed or timed out") from error

    def _loki_path(self, endpoint: str) -> str:
        prefix = ""
        if self.config.type == "grafana":
            uid = quote(self.config.datasource_uid or "", safe="")
            prefix = f"/api/datasources/proxy/uid/{uid}"
        return prefix + "/loki/api/v1/" + endpoint

    @staticmethod
    def _loki_data(result: Any) -> dict[str, Any]:
        if (
            not isinstance(result, dict)
            or result.get("status") != "success"
            or "data" not in result
        ):
            raise ValueError("Loki returned an unsuccessful or malformed response")
        # Query statistics are noisy and can dominate the agent's evidence context.
        if isinstance(result["data"], dict):
            result["data"].pop("stats", None)
        return result

    async def check_health(self) -> None:
        if self.config.type == "grafana":
            health = await self._get("/api/health")
            if not isinstance(health, dict) or health.get("database") != "ok":
                raise ValueError("Grafana database is not healthy")
            uid = quote(self.config.datasource_uid or "", safe="")
            health = await self._get(f"/api/datasources/uid/{uid}/health")
            if not isinstance(health, dict) or health.get("status") != "OK":
                raise ValueError("Grafana Loki datasource is not healthy")
        # The query API, with the exact same credentials/tenant as tools, must work.
        result = self._loki_data(await self._get(self._loki_path("labels")))
        if not isinstance(result["data"], list):
            raise ValueError("Loki returned malformed labels")

    async def list_tools(self, run_context=None, agent=None) -> list[Tool]:
        prefix = self.config.name.replace("-", "_")
        return [
            Tool(
                name=f"{prefix}_query_range",
                description=(
                    "Read production Loki logs using the configured tenant/datasource. "
                    "Use the incident's UTC window, not the current time. "
                    'Example query: {app="sally-billing"}. Results are bounded to 200 lines.'
                ),
                inputSchema=LogQuery.model_json_schema(),
            ),
            Tool(
                name=f"{prefix}_labels",
                description="List Loki label names with the configured tenant/datasource.",
                inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
        ]

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any] | None, meta: dict[str, Any] | None = None
    ) -> CallToolResult:
        prefix = self.config.name.replace("-", "_")
        if tool_name == f"{prefix}_query_range":
            query = LogQuery.model_validate(arguments or {})
            result = await self._get(
                self._loki_path("query_range"),
                {**query.model_dump(), "direction": "backward"},
            )
        elif tool_name == f"{prefix}_labels" and not arguments:
            result = await self._get(self._loki_path("labels"))
        else:
            raise ValueError("unknown read-only observability tool or unsupported arguments")
        result = self._loki_data(result)
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(result))])
