# Handoff — source-control provider extension

Implemented issue #1 with generic provider registration, per-repository publication
dispatch, and mounted webhook routes. External handlers can reach the built application
through `request.app.state.application` and deliver provider-scoped normalized events
through `workflow.handle_source_control_event`. GitHub webhook parsing and conflict
polling remain on the existing `GitHubService`; mixed GitHub and plug-in repositories
are supported. Grouped PR body synchronization filters to GitHub-owned PRs.

Added provider documentation and focused regression coverage. Updated package metadata
and the lockfile workspace entry to version `0.4.0`; the changelog entry remains under
Unreleased pending the release process.

Verification run:

- `uv run pytest --no-cov -q tests/test_source_control_plugins.py tests/test_conflict_polling.py tests/test_github_publishing.py tests/test_api_security.py tests/test_long_horizon.py tests/test_application_verification_gates.py` — 29 passed.
- `uv run ruff check` on the changed Python modules and focused tests — passed.
- `uv run --with pyright pyright src/source_control.py src/app.py src/workflow.py src/github.py` — provider API type check; any remaining report in `workflow.py` is the pre-existing incident-history return typing issue.
- `uv lock --check` and `git diff --check` — passed.

No PR or release was published; the orchestrator owns review and release.
