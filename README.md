# Incident Harness

A durable, long-horizon agent harness that turns production incidents into locally tested,
deployment-verified pull requests. Every intake protocol uses one filesystem-backed workflow, so
tasks remain inspectable and recoverable while the process is running or after a restart.

## What is implemented

- Incident intake over signed HTTP webhooks, MCP-compatible endpoints, A2A endpoints, and JSON files.
- Atomic task queues under `.agent/tasks` with append-only events and SQLite conversation history.
- Incident deduplication and restart recovery without an external queue.
- Per-task Git worktrees backed by one bare mirror per configured repository.
- One durable lead-agent session per task, with stable research and implementation sub-agent
  sessions, automatic context compaction, and global, repository, and task memory.
- Agent-driven lifecycle tools for investigation checkpoints, local verification, durable memory,
  and pull-request publication; the filesystem workflow validates transitions and external waits
  instead of invoking a new model operation for every phase.
- Selectable OpenAI Agents SDK or host-authenticated subscription CLI runtimes, with repository
  instructions, preflight skills, workspace tools, repository graphs, and MCP connector mapping.
- Concise live agent progress in the terminal by default, including bounded reasoning summaries,
  sanitized tool activity, and run status without raw model JSON or complete tool output.
- GitHub webhook signature verification, delivery deduplication, authorized review routing, and
  agent-comment loop prevention.
- Strict deployment matching by repository, environment, and current PR head SHA before Playwright
  is allowed to run against the preview URL.
- Retry budgets, blocked/failed states, cancellation, and merge completion cleanup.
- A Textual configuration editor that persists environment-variable references, never secrets.

## Install and run

Python 3.12 or newer and `uv` are recommended.

```bash
cp .env.example .env        # set up env variables
uv sync
uv run incident-agent init  # recreate the local .agent tree with seed-cli
uv run incident-agent tui   # configure
uv run incident-agent serve
```

The committed `.seed/specs/runtime.tree` is the source of truth for the untracked runtime layout.
`init` creates or repairs the complete `.agent/` skeleton through `seed-cli`. Runtime commands also
run the same bootstrap automatically when `.agent/` is absent, so a fresh checkout is ready on its
first invocation without committing local configuration, sessions, logs, repositories, or tasks.

The runtime loads `.env` from the current working directory without overriding variables already
exported by the shell. The fixed variables are documented in `.env.example`; custom model and MCP
connector credential names come from `.agent/config.toml`.

The installation also includes the repository-intelligence tools `graphify`, `code-review-graph`,
and `seed-cli`. Build both local code graphs for a checkout, or capture a structured filesystem
tree, with:

```bash
uv run incident-agent index /path/to/repository
uv run incident-agent tree --out structure.seed /path/to/repository
```

Graph output stays local in `graphify-out/` and `.code-review-graph/`; `tree` delegates tree
creation to `seed capture`.

Other modes:

```bash
uv run incident-agent serve --no-worker
uv run incident-agent worker
uv run incident-agent mcp
uv run incident-agent run tests/fixtures/incident.json
```

The initialization command creates `.agent/config.toml`. The TUI has separate tabs for model, runtime,
repositories, connections, and safety. In the Model tab choose `local` or `remote`, set the
provider label, model name, OpenAI-compatible base URL, and the names of environment variables
holding credentials. Local mode supports Ollama, vLLM, LM Studio, and similar servers; remote mode
supports OpenAI and hosted compatible APIs. The TUI never asks for or writes secret values.

The same tab selects the execution runtime. `agents-sdk` uses the configured API endpoint and keeps
the task and sub-agent histories in `.agent/sessions.sqlite3`. `subscription-cli` starts `codex exec`
by default and reuses device OAuth already completed by the host CLI. It captures the CLI thread ID,
uses `codex exec resume` after external deployment or review events, translates eligible configured
MCP servers into CLI configuration, and exposes authenticated per-run lifecycle commands. Change
`model.subscription_command` when using another Codex-compatible subscription CLI.

Long SDK sessions compact older items after `model.compaction_threshold` while retaining the newest
`model.session_history_limit` items. The extractive checkpoint is appended to the task's `memory.md`,
which is included together with global and repository memory on every resume. Set
`model.compaction_enabled = false` only when the selected provider handles context compaction itself.

The Safety tab also contains the complete system prompt. That prompt and the positive goals,
negative goals, guardrails, and safeguards are assembled into every investigation, implementation,
and review agent run as a binding instruction contract.

### Durable agent lifecycle

The lead agent receives four stateful harness tools:

- `mark_investigation_complete` stores root cause, evidence, proposed fix, and reproduction status.
- `run_tests` executes and records a real command; failures consume the task retry budget.
- `open_pr` publishes only from a successfully tested state, or updates the known PR head on review.
- `remember` writes task or repository memory that survives process restarts and compaction.

Research and implementation run as bounded sub-agents with stable child session IDs. The lead agent
owns lifecycle transitions and verifies delegated conclusions. Deployment webhooks and authorized
review comments resume the same lead session instead of creating phase-specific conversations.

### Adding skills

The harness searches for nested `SKILL.md` files before every agent operation. Bundled lifecycle
skills are loaded first, then relevant additional skills are selected by matching their name,
description, and triggers against the operation and incident context. Repository-local skills can
be added without Python changes under any of these directories:

```text
skills/<skill-name>/SKILL.md
.agents/skills/<skill-name>/SKILL.md
.claude/skills/<skill-name>/SKILL.md
.codex/skills/<skill-name>/SKILL.md
```

Use frontmatter so the resolver can find the skill quickly:

```markdown
---
name: database-performance
description: "Diagnose slow database access during incidents"
triggers:
  - "slow database query"
  - "query timeout"
---

# Database Performance

Your operating instructions...
```

The directories and automatic-load limit are configurable as `agent.skill_directories` and
`agent.max_auto_skills` in `.agent/config.toml`. Each run records an `agent.skills_resolved` event
with the discovered count and loaded skill names.

Concise agent progress is enabled by default and printed while an agent is running. Raw model JSON,
complete SDK events, tool arguments, and tool output stay out of the console. Use the Model tab's
“Show concise live agent progress in the terminal” checkbox to turn progress off; the setting is
saved in `.agent/config.toml`.

Add at least one repository with its `clone_url` (or `local_path`), accepted incident environments,
preview environment, and Playwright command. Configure trigger mode, webhook security, MCP
connections for incident input, PR output, and observability, plus positive/negative goals,
guardrails, and safeguards from the same TUI. Authentication for an injected GitHub API adapter or
GitHub MCP connector belongs to that adapter and is not a built-in GitHub App setting.

### Local-only repository mode

GitHub is optional. If a checkout is available at either
`.agent/repositories/company--application`, `.agents/repositories/company--application`, or a
configured `local_path`, the harness can discover it without a remote URL. If the repository is not
listed in `config.toml`, the incident's `repository` name is used for this conventional lookup.
The harness creates an isolated branch/worktree, runs the agent and local tests, commits the change
with a local identity, and stores a local PR record at `tasks/completed/<task-id>/pr.json`:

```json
{
  "url": "local://<task-id>",
  "branch": "agent/inc-1842-abc123",
  "head_sha": "..."
}
```

Set `publish_mode = "local"` to require this behavior. The branch remains in the local checkout;
no push, GitHub account, deployment provider, or preview environment is required.

The HTTP service exposes:

```text
GET  /health
POST /hooks/incidents/{connector}
POST /hooks/github
POST /mcp/tools/submit_incident
GET  /mcp/resources/tasks/{task_id}
GET  /mcp/resources/tasks/{task_id}/events
GET  /mcp/resources/tasks/{task_id}/result
POST /mcp/tools/cancel_task/{task_id}
GET  /.well-known/agent-card.json
POST /a2a/tasks
GET  /a2a/tasks/{task_id}
POST /a2a/tasks/{task_id}/cancel
```

## Local execution

To execute the agent locally against a local repo and a local incident when the codebase is configured you can follow the steps outlined below:

```bash
 mkdir -p .agents/repositories
 git clone <repository-url> .agent/repositories/company--application
 uv run incident-agent run PATH_TO_YOUR_FIXTURE
```

The incident’s repository must be company/application. The harness will
create a local branch, commit the fix, and write the result under:

`.agent/tasks/completed/<task-id>/`


## Verification

```bash
uv run pytest
uv run ruff check src tests
uv build
```

The suite covers the vision's minimal test matrix and enforces more than 90% coverage for every
source file as well as at least 90% aggregate coverage.

## Architecture

The implementation uses one asyncio event loop, small responsibility-based modules, filesystem task
queues, SQLite session history, and no additional workflow framework. The workflow owns transition
validation, retries, deployment events, and recovery. Each task's durable lead session decides when
to investigate, delegate, edit, test, remember, and publish by calling the workflow's lifecycle tools.


## Known limitations, pitfalls and non-goals

This was built as a time-boxed prototype (over a 3hr window). And so I left pieces out that would make the agent-harness work better. I list them below in order of importance:

- Stronger tool calling with RL into the main loop, find-research-install tools as needed and retries.
- Evals.
- Logs and observability as primitives.
- Workspaces, guardrails and other safety protocols. Instead relying on the using the .agent folder as the worflow.
- Security.
- Versioning and construction. For the system prompt, skills and connections.
- Extensibility other than by the use of skills.


I have solved some of these pitfalls using graphify and code-review-graph so the agent queries the graph instead of loading the whole codebase into context. This keeps the context window smaller.

I have used skills written by others alongside those that I created for this exercise.
