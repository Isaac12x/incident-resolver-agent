# Incident Dashboard Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development with user-requested Luna workers.

**Goal:** Ship a live read-only incident dashboard with foreground/detached CLI and reversible public routing.
**Architecture:** Dedicated FastAPI server reads SQLite without constructing the agent application. Browser sessions and routing generation state are separate from incident data. Caddy/nginx adapters own only their routing snippets.
**Tech Stack:** Python 3.12, SQLite, FastAPI, uvicorn, packaged HTML/CSS/JavaScript.
**Spec:** docs/superpowers/specs/2026-09-18-dashboard-design.md

## Global Constraints

- Work only in /Users/iamin/work/code/tools/incident-harness-dashboard.
- Preserve unrelated application work in the original checkout; support application fields defensively.
- No new runtime dependencies, incident DB writes, worker startup, or real host proxy changes during tests.
- Public authentication defaults to a dedicated token exchanged for an expiring browser session.
- Controllers integrate and review; workers edit only assigned files and test their behavioral boundaries.

### Task 1: Read-only data and dashboard web experience
Files: src/dashboard/__init__.py, data.py, web.py, assets/*; tests/test_dashboard_data.py, test_dashboard_web.py.
Produces: create_dashboard(root: Path, *, port: int = 8766, nonce: str = "") -> FastAPI.
Consumes: runtime SQLite and dashboard-owned public.json maintained by routing adapters.
- [x] Test incident/completion/failure counts, completion-event timing, unique PRs, missing DB, filters and pagination.
- [x] Implement read-only snapshots, safe event projection, live SSE and token/session authentication.
- [x] Build responsive overview cards, filtered task table and detail timeline with clear metric definitions.
- [x] Test unauthorized/proxied requests, revocation, unavailable data and packaged assets.

### Task 2: CLI and process lifecycle
Files: src/dashboard/cli.py, process.py; src/__main__.py; tests/test_dashboard_cli.py, test_dashboard_process.py.
Consumes: create_dashboard above; routing CLI dispatch from Task 3.
Produces: add_dashboard_parser(commands); run_dashboard_command(args).
- [x] Test parser aliases, foreground/detached behavior, duplicate start, stale PID and port conflicts.
- [x] Dispatch dashboard before Application.build and any readiness/bootstrap work.
- [x] Implement start/status/stop/logs with runtime owner metadata, startup locking, readiness and safe identity checks.
- [x] Exercise a real subprocess start/status/stop and preserve incident data.

### Task 3: Caddy/nginx adapters
Files: src/dashboard/routing.py; tests/test_dashboard_routing.py.
Produces: add_port_arguments(parser); run_port_command(args, root: Path).
- [x] Test host/port validation, detection ambiguity, conflicts, permission and unsupported layout failures.
- [x] Implement owned snippets, full config validation, journaled apply/reload/rollback and idempotence.
- [x] Revoke public generation before close, including failed cleanup and stale streams.
- [x] Test adapter rendering/commands in disposable fixtures; use real proxy executables when available.

### Task 4: Integration and publication
Files: README.md, CHANGELOG.md, plan/spec, handoff.md, packaging if needed, integration tests.
- [x] Independently review each task for spec compliance and correctness; route fixes to its worker.
- [x] Run full pytest coverage, Ruff, build, installed-wheel smoke, browser checks and process smoke.
- [x] Document precise metric semantics and supported proxy configuration; report unexercised live infrastructure.
- [ ] Commit scoped changes, push feat/incident-dashboard and create PR stacked on application-scoping PR #21 once integrated.
- [ ] Check comments/reviews initially and after five minutes; integrate authorized feedback.

## Execution ledger

- Ruling: use dedicated token authentication as previously recommended; user authorized implementation and accepted recommendations. A different login preference would require changing only dashboard auth.
- Ruling: isolate from concurrent uncommitted application changes; initially tolerate their fields, then integrate committed application-scoping base 6d954f2 (including 7a0873a) before final verification.

## Verification results

- Full suite: 499 passed, 94.24% overall coverage; all repository per-file gates pass.
- Dashboard module coverage: CLI 92%, data 94%, process 95%, routing 91%, web 96%.
- Ruff, JavaScript syntax, compile checks, lockfile check, wheel and source build pass.
- Installed-wheel CLI detach/status/stop, authentication, assets, and missing-runtime read-only checks pass.
- Chromium desktop/mobile checks pass for metrics, filtered live updates, detail revisions,
  pagination, empty results, redaction, logout, and responsive layout; no JavaScript errors.
- Disposable real Caddy and nginx tests pass for TLS login, snapshots, SSE, existing routes,
  publication/close, and revocation. No production host routing or internet exposure was deployed.
- Independent final review has no remaining important or critical findings. Protected route
  bindings are retired before reusable files are removed; stale journals cannot affect a later route.
