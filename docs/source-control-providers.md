# Source-control provider extensions

Core includes GitHub support and can run without any third-party provider package. An
installed package can register a provider factory and mount its own FastAPI webhook
routes before building the application and server.

```python
from fastapi import APIRouter, Request

from src.source_control import register_source_control_provider, register_webhook_router


class ExampleProvider:
    publish_enabled = True

    def __init__(self, config, storage):
        self.config = config
        self.storage = storage

    async def create_pull_request(self, task): ...
    async def update_pull_request(self, task): ...
    async def publish_verification(self, task, result): ...
    def repository_and_pr(self, payload): ...
    def review_comment(self, payload): ...
    async def authenticate_and_translate(self, request): ...


register_source_control_provider("example", ExampleProvider)

router = APIRouter()


@router.post("/hooks/example")
async def example_webhook(request: Request):
    application = request.app.state.application
    provider = application.source_control["example"]
    # This provider method verifies the request signature before returning normalized data.
    event, payload = await provider.authenticate_and_translate(request)
    task = await application.workflow.handle_source_control_event("example", event, payload)
    return {"accepted": True, "task_id": task.task_id if task else None}


register_webhook_router(router)
```

Set a repository's `publish_mode` to the registered name (`example`) to select that
provider. `publish_mode` also accepts `auto`, `github`, and `local`. Each repository
selects its provider independently; `auto` and `github` continue to use the core
`GitHubService`. The factory receives the loaded `Config` and runtime `Storage`.

Provider implementations expose a `publish_enabled` boolean and implement
`create_pull_request(task)`, `update_pull_request(task)`, and
`publish_verification(task, result)`. The first two return a `PullRequestReference`
(or `None` when an update is not confirmed); verification publication returns `None`.
The provider receives the core `TaskRecord` and `VerificationResult` models. For
webhook delivery, implement `repository_and_pr(payload)` and
`review_comment(payload)` for your provider's translated event shape. The event name
and payload should use the core workflow's existing pull request, comment, merge and
deployment fields. Implement `authenticate_and_translate(request)` on the provider
used by the route. It must verify provider authentication before returning normalized
event data; do not dispatch unverified payloads.

Import and register the extension before calling `Application.build` or
`create_server`. Mounted handlers can access the built application as
`request.app.state.application`. Core keeps `/hooks/github` and its GitHub signature,
delivery de-duplication, event handling, and conflict polling on `GitHubService`.
