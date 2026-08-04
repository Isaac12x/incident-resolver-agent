"""HTTP, MCP-compatible, and A2A protocol surfaces."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .app import Application
from .github import WebhookSignatureError
from .models import Incident


def create_server(application: Application, *, run_worker: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await application.connectors.start()
        worker = asyncio.create_task(application.workflow.run_worker()) if run_worker else None
        yield
        application.workflow.stop()
        if worker:
            await worker
        await application.connectors.stop()

    server = FastAPI(title="Incident Harness", version="0.1.0", lifespan=lifespan)

    @server.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    incident_hook_path = f"{application.config.trigger.hook_path}/{{connector}}"

    @server.post(incident_hook_path, status_code=status.HTTP_202_ACCEPTED)
    async def incident_webhook(
        connector: str, request: Request, x_agent_signature_256: str = Header(default="")
    ) -> dict[str, str]:
        body = await request.body()
        secret = os.getenv(application.config.server.webhook_secret_env)
        if secret:
            expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, x_agent_signature_256):
                raise HTTPException(status_code=401, detail="invalid webhook signature")
        try:
            payload = await request.json()
            incident = application.connectors.normalize_incident(connector, payload)
            task = await application.workflow.submit(incident)
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {"task_id": task.task_id, "state": task.state.value}

    @server.post("/hooks/github", status_code=status.HTTP_202_ACCEPTED)
    async def github_webhook(request: Request) -> JSONResponse:
        body = await request.body()
        try:
            application.github.verify_webhook(request.headers, body)
        except WebhookSignatureError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        delivery = request.headers.get("x-github-delivery", "")
        if not application.github.accept_delivery(delivery):
            return JSONResponse({"duplicate": True}, status_code=status.HTTP_202_ACCEPTED)
        payload = application.github.decode(body)
        event = request.headers.get("x-github-event", "")
        task = await application.workflow.handle_github_event(event, payload)
        return JSONResponse(
            {"accepted": True, "task_id": task.task_id if task else None},
            status_code=status.HTTP_202_ACCEPTED,
        )

    async def submit(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            task = await application.workflow.submit(Incident.model_validate(payload))
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return task.model_dump(mode="json")

    @server.post("/mcp/tools/submit_incident")
    async def mcp_submit(payload: dict[str, Any]) -> dict[str, Any]:
        return await submit(payload)

    @server.get("/mcp/resources/tasks/{task_id}")
    async def mcp_task(task_id: str) -> dict[str, Any]:
        try:
            return application.storage.load_task(task_id).model_dump(mode="json")
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="task not found") from error

    @server.get("/mcp/resources/tasks/{task_id}/events")
    async def mcp_events(task_id: str) -> list[dict[str, Any]]:
        try:
            return [event.model_dump(mode="json") for event in application.storage.events(task_id)]
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="task not found") from error

    @server.get("/mcp/resources/tasks/{task_id}/result")
    async def mcp_result(task_id: str) -> dict[str, Any]:
        try:
            task = application.storage.load_task(task_id)
            path = application.storage.task_directory(task_id) / "result.md"
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="task not found") from error
        return {
            "task_id": task_id,
            "state": task.state.value,
            "result": path.read_text(encoding="utf-8") if path.exists() else None,
        }

    @server.post("/mcp/tools/cancel_task/{task_id}")
    async def mcp_cancel(task_id: str) -> dict[str, Any]:
        try:
            return application.workflow.cancel(task_id).model_dump(mode="json")
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="task not found") from error

    @server.get("/.well-known/agent-card.json")
    async def agent_card() -> dict[str, Any]:
        return {
            "name": "Incident Harness",
            "description": "Investigates incidents and delivers verified pull requests",
            "url": application.config.server.public_url or "/a2a",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "skills": [{"id": "resolve-incident", "name": "Resolve incident"}],
        }

    @server.post("/a2a/tasks")
    async def a2a_submit(payload: dict[str, Any]) -> dict[str, Any]:
        return await submit(payload.get("incident", payload))

    @server.get("/a2a/tasks/{task_id}")
    async def a2a_get(task_id: str) -> dict[str, Any]:
        return await mcp_task(task_id)

    @server.post("/a2a/tasks/{task_id}/cancel")
    async def a2a_cancel(task_id: str) -> dict[str, Any]:
        return await mcp_cancel(task_id)

    return server
