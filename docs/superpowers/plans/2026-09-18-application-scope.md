# Application-scoped incident repair implementation plan

**Goal:** One durable agent investigates and repairs an application across repositories.
**Architecture:** Explicit application membership resolves intake into a snapshotted task scope.
Per-repository state and worktrees feed one session, with aggregate publication and verification.
**Tech stack:** Existing Python, Pydantic, SQLite, Git, Agents SDK/subscription CLI.
**Spec:** `docs/superpowers/specs/2026-09-18-application-scope-design.md`

## Global constraints

Preserve repository-only tasks and existing user edits. No dependencies. Do not weaken safety,
verification, retry budgets, recovery, or exact-SHA deployment requirements. Parent owns commits
and PRs; agents own disjoint implementation files and new focused tests. User explicitly requests
Luna implementation agents and parent orchestration/verification, then a fresh Luna docs agent.

## Shared interfaces

- `ApplicationConfig`: `name`, `services: list[str]`, `repositories: list[str]`,
  `integration_command: str = ''`. `Config.applications` and `Config.application(name)`.
- `Incident`: optional `application`, `service`; legacy `repository` defaults to empty for routing.
- `RepositoryTaskState`: `repository`, `branch`, PR/deployment/code-review fields corresponding to
  existing `TaskRecord`, and per-repository review comments and verification progress as needed.
- `TaskRecord`: optional `application`, `service`, and `repositories: dict[str, RepositoryTaskState]`.
  Empty mapping means legacy behavior. Repository is the compatibility primary target.
- Storage exposes `repository_worktree(task, repository=None)`,
  `create_application_worktrees(task, config)`, and repository-aware commit/diff/cleanup helpers.
- Application worktrees live under `worktrees/<task-id>/<safe-repository-name>`; legacy worktree
  layout remains unchanged. The application parent is the session working directory.
- Lifecycle `run_tests` and `verification_plan` accept optional `repository`; application
  integration checks use the parent workspace and bind evidence to every repository revision.
- Agents coordinate any interface refinement directly and report it to the parent before use.

## Task 1: Scope, intake and durable storage (Luna foundation)

Files: config.py, models.py, connectors.py, app.py, storage.py, task_catalog.py, focused new tests.
- [x] Test explicit application/service routing, ambiguous overlap, legacy routing and round trips.
- [x] Implement validated membership and intake normalization; preserve old incident identity.
- [x] Persist repository states, scope deduplication and PR lookup across all targets.
- [x] Prepare/recover isolated worktrees and verify identities, commit/diff/cleanup per target.
- [x] Run focused tests and report exact public interfaces to other implementers.

## Task 2: Coordinated lifecycle (Luna workflow)

Files: workflow.py, github.py, verify.py, optional new application workflow module, focused tests.
- [x] Test partial publication/restart, changed-only PRs and per-repository event routing.
- [x] Resolve intake via configured application scope and snapshot it in the created task.
- [x] Adapt preparation, local checks, publication, review and deployment to all targets.
- [x] Require current verification for all changed targets plus configured integration command;
  invalidate integration evidence when any revision changes. Keep one durable agent session.
- [x] Run focused lifecycle tests including legacy regressions and report interfaces.

## Task 3: Shared agent tools and configuration UI (Luna agent tools)

Files: agent.py, tools.py, execution.py, tui.py, optional application tools module, focused tests.
- [x] Test one session can read/write/run commands in either member but cannot escape its scope.
- [x] Assemble instructions, graphs, skills and memories with clear repository provenance.
- [x] Add repository selection to SDK tools and subscription bridge and lifecycle forwarding.
- [x] Ensure container mounts/protected files enforce policy for every member.
- [x] Expose application membership in configuration UI and add focused tests.

## Task 4: Parent integration verification

- [x] Review interfaces and full diff against the spec; send fixes back to owning Luna agents.
- [x] Run a regression with two real Git repos, publication recovery and stale evidence rejection.
- [x] Run full pytest coverage gates, Ruff, compileall, lock check and package build.
- [x] Inspect final scope, compatibility and security paths; record any unexercised live behavior.

## Task 5: Fresh Luna documentation, then parent delivery

- [x] After implementation, launch a new Luna agent for README, configuration docs and changelog.
- [ ] Parent verifies docs, appends handoff, commits only owned changes and opens a PR.
- [ ] Check review feedback initially and after 5–10 minutes; integrate authorized corrections.
