# Handoff

## 2026-08-16 — Durable long-horizon agent sessions

Branch: `feat/durable-agent-sessions`

Implemented one resumable lead-agent session per incident task with stable research and
implementation child sessions. The production workflow now exposes validated lifecycle tools for
investigation completion, local test execution, memory, and PR publication; external deployment and
review events resume the same session. Legacy injected backends retain their phase entry points for
compatibility, but the Agents SDK and subscription CLI use the durable path.

Added task memory and bounded SQLite session compaction. Added a selectable host-authenticated
subscription CLI backend (Codex by default) with structured result schemas, thread ID persistence and
resume, MCP configuration translation, a read-only CLI sandbox, and an authenticated bridge for
permission-checked workspace and lifecycle tools.

Documentation, TUI configuration, changelog, and regression coverage were updated.

Verification completed:

- `uv run pytest` — 79 passed, 95.47% aggregate coverage; every source file above 90%.
- `uv run ruff check src tests` — passed.
- `uv run python -m compileall -q src tests` — passed.
- `uv build` — passed.
- `uv run --with 'mypy>=1.11' mypy src --ignore-missing-imports` — not clean because of 30
  pre-existing typing issues in config defaults, connector SDK types, TUI literal narrowing, and
  legacy workflow result variable reuse. The new agent tool-list invariance findings were corrected.

Published as [PR #1](https://github.com/Isaac12x/incident-resolver-agent/pull/1). Six polls over
five minutes confirmed the PR was mergeable with no configured checks, preview deployment,
reviews, or comments. Because GitHub supplied no deployment SHA, required environment, or preview
URL, deployment verification was correctly left unclaimed.
