"""Read-only local log connector, without an observability service dependency."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from typing import Any

from agents.mcp import MCPServer
from mcp.types import CallToolResult, GetPromptResult, ListPromptsResult, TextContent, Tool
from pydantic import BaseModel, ConfigDict, Field

from .config import ConnectorConfig

MAX_READ_BYTES = 1_000_000
MAX_OUTPUT_BYTES = 64_000


class LocalLogQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contains: str = Field(default="", max_length=4000, description="Case-sensitive literal filter")
    limit: int = Field(default=100, ge=1, le=200)


class LocalLogServer(MCPServer):
    def __init__(self, config: ConnectorConfig) -> None:
        super().__init__()
        self.config = config
        self.path = Path(config.log_path or "").expanduser()

    @property
    def name(self) -> str:
        return self.config.name

    async def connect(self) -> None:
        # Discover tools even while the application has not yet created its log.
        pass

    async def cleanup(self) -> None:
        pass

    async def list_prompts(self) -> ListPromptsResult:
        return ListPromptsResult(prompts=[])

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> GetPromptResult:
        raise ValueError("local log connectors do not provide prompts")

    def _read(self, query: LocalLogQuery | None = None) -> dict[str, Any]:
        # Nonblocking open prevents a FIFO/device from hanging a worker or health check.
        descriptor = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("local log path must be a regular file")
            if query is None:
                return {}
            start = max(0, info.st_size - MAX_READ_BYTES)
            os.lseek(descriptor, start, os.SEEK_SET)
            raw = os.read(descriptor, MAX_READ_BYTES)
        finally:
            os.close(descriptor)
        if start:
            # Never return a fragment as if it were a complete log record.
            raw = raw.partition(b"\n")[2]
        matches = [
            line
            for line in raw.decode("utf-8", errors="replace").splitlines()
            if query.contains in line
        ]
        selected: list[str] = []
        output_bytes = 0
        for line in reversed(matches[-query.limit :]):
            encoded = line.encode("utf-8")
            if output_bytes + len(encoded) > MAX_OUTPUT_BYTES:
                break
            selected.append(line)
            output_bytes += len(encoded)
        return {
            "path": str(self.path),
            "lines": list(reversed(selected)),
            "truncated": start > 0 or len(selected) < len(matches),
            "scanned_bytes": len(raw),
            "scope": "recent file tail; timestamps are not parsed or filtered",
        }

    async def check_health(self) -> None:
        await asyncio.to_thread(self._read)

    async def list_tools(self, run_context=None, agent=None) -> list[Tool]:
        return [
            Tool(
                name=f"{self.name.replace('-', '_')}_read_logs",
                description=(
                    "Read recent lines from the configured application log on the harness host. "
                    "Optional literal filter; at most 200 lines / 64 KB from the last 1 MB. "
                    "Results are chronological. Check timestamps against the incident window; "
                    "older evidence may have rotated out. The file path cannot be overridden."
                ),
                inputSchema=LocalLogQuery.model_json_schema(),
            )
        ]

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any] | None, meta: dict[str, Any] | None = None
    ) -> CallToolResult:
        if tool_name != f"{self.name.replace('-', '_')}_read_logs":
            raise ValueError("unknown read-only local log tool")
        query = LocalLogQuery.model_validate(arguments or {})
        result = await asyncio.to_thread(self._read, query)
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(result))])
