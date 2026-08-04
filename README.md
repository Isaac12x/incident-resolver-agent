# Incident Harness

A durable, single-agent harness that turns production incidents into locally tested,
deployment-verified pull requests. Every intake protocol uses one filesystem-backed workflow, so
tasks remain inspectable and recoverable while the process is running or after a restart.

## What is implemented

- Incident intake over signed HTTP webhooks, MCP-compatible endpoints, A2A endpoints, and JSON files.
- Atomic task queues under `.agent/tasks` with append-only events and SQLite conversation history.
- Incident deduplication and restart recovery without an external queue.
- Per-task Git worktrees backed by one bare mirror per configured repository.
- A bounded OpenAI Agents SDK coding runtime with repository instructions, skills, memory, MCP tool
  adapters, workspace-constrained file tools, and any local or hosted OpenAI-compatible endpoint.
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
cp .env.example .env
uv sync
uv run incident-agent tui
uv run incident-agent serve
```

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

The first command creates `.agent/config.toml`. The TUI has separate tabs for model, runtime,
repositories, connections, and safety. In the Model tab choose `local` or `remote`, set the
provider label, model name, OpenAI-compatible base URL, and the names of environment variables
holding credentials. Local mode supports Ollama, vLLM, LM Studio, and similar servers; remote mode
supports OpenAI and hosted compatible APIs. The TUI never asks for or writes secret values.

The Safety tab also contains the complete system prompt. That prompt and the positive goals,
negative goals, guardrails, and safeguards are assembled into every investigation, implementation,
and review agent run as a binding instruction contract.

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

## Verification

```bash
uv run pytest
uv run ruff check src tests
uv build
```

The suite covers the vision's minimal test matrix and enforces more than 90% coverage for every
source file as well as at least 90% aggregate coverage.

## Architecture

The implementation follows [DESIGN.md](DESIGN.md): one asyncio event loop, small responsibility-based
modules, filesystem task queues, SQLite only for model conversation sessions, and no additional
workflow framework or service layers.

I avoided creating a whole memory system and the need for compaction as I'm treating the agent loop as ephimeral and the memory itself as lookup + insert in context.
