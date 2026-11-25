# Source-control provider extensions

Core includes GitHub support and can run without any third-party provider package.
An installed package can register a provider factory and mount its own FastAPI webhook
routes. A zero-argument registrar can be discovered from the
`incident_harness.plugins` entry-point group:

```python
from src.source_control import (
    WebhookEvent,
    register_source_control_provider,
    register_webhook_router,
)


def register():
    register_source_control_provider("example", ExampleProvider)
    register_webhook_router(router)
```

Declare the registrar in the package metadata:

```toml
[project.entry-points."incident_harness.plugins"]
example = "example_provider:register"
```

`ExampleProvider` implements the provider operations below. Its `router` defines its
HTTP endpoint and calls `authenticate_and_translate` before dispatching events.

Set a repository's `publish_mode` to the registered name (`example`) to select that
provider. `publish_mode` also accepts `auto`, `github`, and `local`. Each repository
selects its provider independently; `auto` and `github` continue to use the core
GitHub provider. The factory receives the loaded `Config` and runtime `Storage`.

Provider implementations expose a `publish_enabled` boolean and implement
`create_pull_request(task)`, `update_pull_request(task)`, and
`publish_verification(task, result)`. The first two return a `PullRequestReference`
(or `None` when an update is not confirmed); verification publication returns `None`.
The provider receives the core `TaskRecord` and `VerificationResult` models. For
webhook delivery, implement `repository_and_pr(payload)` and
`review_comment(payload)` for your provider's translated event shape. The event name
and payload should use the core workflow's existing pull request, comment, merge and
deployment fields. The workflow matches repository identity fields to the configured
`name` or the full repository path in its remote `clone_url`. For a checkout configured
only by `local_path`, a provider that receives a different remote name must translate
the repository fields and PR target to the configured name. A PR number alone cannot
distinguish repositories that use the same provider.

Implement this synchronous webhook contract on the provider used by the route:

```python
def authenticate_and_translate(headers: Mapping[str, str], body: bytes) -> WebhookEvent: ...
```

`WebhookEvent` is a named tuple with `event`, `payload`, and `duplicate` fields, so it
also remains compatible with tuple unpacking. Verify authentication against the exact
raw body before decoding or translating it. Raise `WebhookSignatureError` for missing
or invalid credentials.
Each provider's HTTP route owns its request-size limit and status mapping. The native
`/hooks/github` route caps bodies at 1 MiB, maps authentication failures to HTTP 401,
and acknowledges duplicates with HTTP 202 and `{"duplicate": true}` without dispatch.
It calls the `github` provider through this contract, which owns GitHub signature
verification, delivery de-duplication, header translation, and JSON decoding. GitHub
health checks continue to use its API adapter.

Register the extension before validating a `RepositoryConfig` directly. `load_config`
discovers installed entry points when the TOML selects a non-builtin provider; built-in
configuration reads do not import provider packages. `Application.build` and
`create_server` also run discovery so installed webhook routers are mounted, including
when the server is created from an existing application. Mounted handlers can access
the built application as `request.app.state.application`. Core keeps
`/hooks/github`, event handling, and conflict polling on the registered GitHub provider.

Discovery runs once per process and is safe when application builds or server creation
happen concurrently. Each entry-point registrar runs in its own registration transaction:
if loading or registration fails, core logs the failure, removes that registrar's partial
provider and router registrations, and continues with later extensions. Built-in providers
and routes remain available. Configuration validation fails before application build if a
repository selects a provider that was not registered.
