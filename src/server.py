"""HTTP, MCP-compatible, and A2A protocol surfaces."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .app import Application
from .github import GitHubCLIAdapter, WebhookSignatureError
from .models import Incident

MAX_WEBHOOK_BODY_BYTES = 1_048_576
MAX_INTELLIGENCE_TEXT_BYTES = 32 * 1024


async def _bounded_request_body(request: Request, maximum: int = MAX_WEBHOOK_BODY_BYTES) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum:
            raise HTTPException(status_code=413, detail="request body is too large")
    return bytes(body)


def _intelligence_text(payload: dict[str, Any], *keys: str) -> str:
    value = next((payload.get(key) for key in keys if payload.get(key) is not None), None)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail="text is required")
    if len(value.encode("utf-8")) > MAX_INTELLIGENCE_TEXT_BYTES:
        raise HTTPException(status_code=422, detail="text is too large")
    return value.strip()


def create_server(application: Application, *, run_worker: bool = True) -> FastAPI:
    worker_task: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal worker_task
        await application.connectors.start()
        worker_task = asyncio.create_task(application.workflow.run_worker()) if run_worker else None
        yield
        application.workflow.stop()
        if worker_task:
            await worker_task
        await application.connectors.stop()

    server = FastAPI(title="Incident Harness", version="0.1.0", lifespan=lifespan)

    @server.middleware("http")
    async def authenticate_control_api(request: Request, call_next):
        path = request.url.path
        if path == "/mcp" or path.startswith("/mcp/") or path.startswith("/a2a/"):
            token = os.getenv(application.config.server.api_token_env)
            if application.config.server.require_api_auth and not token:
                return JSONResponse({"detail": "API authentication is not configured"}, 503)
            if token:
                authorization = request.headers.get("authorization", "")
                scheme, _, supplied = authorization.partition(" ")
                if scheme.lower() != "bearer" or not hmac.compare_digest(
                    supplied.encode(), token.encode()
                ):
                    return JSONResponse(
                        {"detail": "invalid API credentials"},
                        401,
                        headers={"WWW-Authenticate": "Bearer"},
                    )
        return await call_next(request)

    @server.get("/health")
    async def health() -> JSONResponse:
        connector_errors = dict(application.connectors.errors)
        connections: dict[str, dict[str, str]] = {}
        results, github_results = await asyncio.gather(
            application.connectors.health(),
            application.github.api.check_health()
            if isinstance(application.github.api, GitHubCLIAdapter)
            else asyncio.sleep(0, result={}),
        )
        for name, result in results.items():
            connections[name] = {"status": "ok" if result.connected else "failed"}
            if result.connected:
                connector_errors.pop(name, None)
            else:
                connector_errors[name] = result.message
                connections[name]["error"] = result.message
        connections.update(github_results)
        for name, result in github_results.items():
            if result["status"] != "ok":
                connector_errors[name] = result["error"]
        worker_status = "external"
        worker_error: str | None = None
        if run_worker:
            if worker_task is None:
                worker_status = "starting"
            elif worker_task.done():
                worker_status = "failed"
                if not worker_task.cancelled() and (error := worker_task.exception()):
                    worker_error = str(error)
            else:
                worker_status = "running"
        ready = not connector_errors and worker_status not in {"starting", "failed"}
        body: dict[str, Any] = {
            "status": "ok" if ready else "unavailable",
            "worker": worker_status,
            "connectors": "ok" if not connector_errors else "failed",
        }
        if connections:
            body["connections"] = connections
        if connector_errors:
            body["connector_errors"] = connector_errors
        if worker_error:
            body["worker_error"] = worker_error
        return JSONResponse(body, status_code=200 if ready else 503)

    incident_hook_path = f"{application.config.trigger.hook_path}/{{connector}}"

    @server.post(incident_hook_path, status_code=status.HTTP_202_ACCEPTED)
    async def incident_webhook(
        connector: str,
        request: Request,
        x_agent_signature_256: str = Header(default=""),
        x_grafana_alerting_signature: str = Header(default=""),
    ) -> dict[str, Any]:
        body = await _bounded_request_body(request)
        secret = os.getenv(application.config.server.webhook_secret_env)
        if application.config.server.require_api_auth and not secret:
            raise HTTPException(status_code=503, detail="webhook authentication is not configured")
        if secret:
            digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            provided = x_agent_signature_256 or x_grafana_alerting_signature
            if provided.startswith("sha256="):
                provided = provided.removeprefix("sha256=")
            if not hmac.compare_digest(digest, provided):
                raise HTTPException(status_code=401, detail="invalid webhook signature")
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("webhook payload must be an object")
            if connector not in application.connectors.configs:
                raise ValueError(f"connector is not configured: {connector}")
            event_metadata = application.storage.record_observability_event(connector, payload)
            alerts = payload.get("alerts")
            if (
                isinstance(alerts, list)
                and alerts
                and not any(
                    isinstance(alert, dict) and alert.get("status") == "firing" for alert in alerts
                )
            ):
                return {"task_id": "", "state": "ignored"}
            incident = application.connectors.normalize_incident(connector, payload)
            task = await application.workflow.submit(incident)
            application.storage.attach_event_task(str(event_metadata["event_id"]), task.task_id)
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {
            "task_id": task.task_id,
            "state": task.state.value,
            "event_id": event_metadata.get("event_id"),
            "group_key": event_metadata.get("group_key"),
            "duplicate": event_metadata.get("duplicate", False),
        }

    @server.post("/hooks/github", status_code=status.HTTP_202_ACCEPTED)
    async def github_webhook(request: Request) -> JSONResponse:
        body = await _bounded_request_body(request)
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
        except (TypeError, ValueError) as error:
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

    @server.get("/mcp/resources/intelligence/events")
    async def intelligence_events(
        source: str | None = None, group_key: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        try:
            return application.storage.list_observability_events(
                source=source, group_key=group_key, limit=limit
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @server.get("/mcp/resources/tasks/{task_id}/summary")
    async def intelligence_summary(task_id: str) -> dict[str, Any]:
        try:
            return application.workflow.intelligence_summary(task_id)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="task not found") from error

    @server.post("/mcp/tools/predict_root_cause")
    async def predict_root_cause(payload: dict[str, Any]) -> dict[str, Any]:
        text = _intelligence_text(payload, "text", "summary")
        return await asyncio.to_thread(application.workflow.predict_root_cause, text)

    @server.post("/mcp/tools/search_similar_incidents")
    async def search_similar_incidents(payload: dict[str, Any]) -> dict[str, Any]:
        text = _intelligence_text(payload, "text", "query")
        try:
            limit = int(payload.get("limit", 5))
            if not 1 <= limit <= 20:
                raise ValueError("limit must be between 1 and 20")
            return await asyncio.to_thread(
                application.workflow.similar_incidents_search, text, limit
            )
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @server.post("/mcp/tools/rebuild_intelligence")
    async def rebuild_intelligence() -> dict[str, Any]:
        return await asyncio.to_thread(application.workflow.rebuild_intelligence)

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
