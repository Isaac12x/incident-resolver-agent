# Application-scoped incident repair

An application is an explicit incident scope that joins named services to one or more configured
repositories. It lets one durable lead-agent session investigate and repair a cross-repository
incident while preserving each repository's worktree, instructions, graph, permissions, branch,
pull request, review state, and verification evidence.

## Configuration

Define repositories first, then reference their exact configured names from an `[[applications]]`
table. This example joins a Python backend and a React frontend. In TOML, `services` and
`repositories` are arrays; the corresponding TUI fields are comma-separated. `integration_command`
is optional.

```toml
[[repositories]]
name = "acme/backend"
local_path = "/srv/repos/backend"
publish_mode = "github"
base_branch = "main"
incident_environments = ["production"]
verification_environment = "preview"

[[repositories]]
name = "acme/frontend"
local_path = "/srv/repos/frontend"
publish_mode = "github"
base_branch = "main"
incident_environments = ["production"]
verification_environment = "preview"

[[applications]]
name = "checkout"
services = ["checkout-api", "checkout-web"]
repositories = ["acme/backend", "acme/frontend"]
integration_command = "python acme--backend/tests/integration.py acme--frontend"
```

The sample is intentionally explicit: repository names must already exist in `repositories`, and
an application must contain at least one repository. The storage-safe child directory names are
`acme--backend` and `acme--frontend`, which is why the integration command uses that path. The
mapping replaces `/` with `--` and sanitizes other unsafe characters. Configuration rejects
repository identifiers that collide in durable storage, and the runtime validates this mapping
before using a worktree.

The TUI exposes the same fields in **Applications**: application name, comma-separated services,
comma-separated member repositories, and an optional integration command. Ensure member names are
also present in the **Repositories** tab. Configuration is stored under the normal
`.agent/config.toml` or configured per-user path; secret values are not written there.

## Intake routing and task identity

An incident can name an `application`, a `service`, or a `repository`. An explicit application is
checked against any supplied service and repository. Without an application name, a service or
repository must resolve to exactly one configured application. If applications overlap, an
implicit match is ambiguous and intake fails closed. A missing routing hint is also rejected when
applications are configured; repository-only intake remains available when no application scope is
selected. Connectors accept application/service-only payloads, while Grafana labels can provide
`application`/`app`, `service`, and `repository`/`repo`.

An application does not merge unrelated alerts. Deduplication still uses the incident source,
external ID, environment, and application scope. A created application task snapshots its member
repositories, so later configuration edits do not change the task's authorization boundary. A
repository-only task keeps the legacy empty repository-state mapping and remains recoverable from
existing state.

## One session, isolated repositories

The lead session uses the application parent workspace at
`<runtime_root>/worktrees/<task-id>` (`.agent/worktrees/<task-id>` by default). Each member has an
isolated child checkout below that directory.
Repository-aware tools take a `repository` selector for `shell`, `read_file`, `write_file`,
`replace_in_file`, conversation search, graph search/query/impact, `run_tests`, and
`verification_plan`. Omitting the selector uses the primary repository for compatibility. A tool
cannot select a repository outside the snapshotted scope, escape its selected checkout, or access
control directories such as `.git`, `.agent`, and (unless permitted) `.github`. Workspace identity
checks also reject a replaced or moved member checkout during a run.

The parent directory is a session working directory, not another repository. The optional
`integration_command` runs with that parent as its current directory, so it can refer to all member
checkouts by their safe child names. The command is bounded by the same execution and permission
policy and is recorded as application evidence tied to the complete current revision and file set.

## Verification, publication, and review

Each changed repository must pass its own configured local verification before publication. The
verification ledger records repository-relative paths, commands, and input hashes; stale evidence
is invalidated when inputs change. Only changed repositories receive publication. A clean member
does not get an empty PR merely because it belongs to the application.

GitHub publication checkpoints after every repository. A temporary failure can therefore leave one
PR published and resume the remaining repositories after a restart without creating a duplicate
PR. The native GitHub adapter then updates each sibling PR body with the other published PR URLs,
numbers, and exact head SHAs. A custom GitHub adapter can implement the optional
`sync_application_pull_requests` hook for equivalent linking; there is no automatic transaction
that rolls back a repository already published when another repository fails.

Remote deployment verification is per repository and requires the configured environment and the
current PR head SHA before Playwright runs. Review comments are retained per repository, routed
back into the same durable lead session, and survive worker restarts. A new review change reruns
the affected repository's local checks and publication before deployment verification is accepted.
The task can complete only after every changed repository has current required evidence and any
configured application integration check passes.

For `publish_mode = "local"` (or `auto` without a GitHub adapter), publication creates a local
commit and durable local result. It does not create a remote PR or claim preview/Playwright
validation. Remote repositories retain their per-repository deployment and exact-SHA gates.

The integration result is cached against the command plus every member's current revision and
input-file snapshot. A change in either repository invalidates that result. An empty integration
command means that no cross-repository command is required; per-repository checks still apply.

## Compatibility and limits

Existing repository-only configuration, intake payloads, task folders, and durable sessions remain
supported without manual migration; startup migrates existing task snapshots into the internal
catalog as needed. Add `[[applications]]` only for an explicitly scoped workflow; do not assume
that sharing a repository or application makes unrelated incidents one task. The current
implementation coordinates publication and evidence across repositories but does not provide an
automatic cross-repository deployment transaction. Local publication does not validate a live
preview, and this documentation does not claim live model or preview verification.
