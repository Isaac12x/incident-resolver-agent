# Incident Harness

A durable, long-horizon agent harness that turns production incidents into locally tested,
deployment-verified pull requests. Every intake protocol uses one SQLite-backed workflow, so
tasks remain inspectable and recoverable while the process is running or after a restart.

## What is implemented

- Incident intake over signed HTTP webhooks, MCP-compatible endpoints, A2A endpoints, and JSON files.
- Transactional task state and event history in SQLite, readable `.agent/tasks` artifacts, and conversation history.
- Incident deduplication and restart recovery without an external queue.
- Per-task Git worktrees backed by one bare mirror per configured repository.
- One durable lead-agent session per task, with stable research and implementation sub-agent
  sessions, automatic context compaction, and global, repository, and task memory.
- Agent-driven lifecycle tools for investigation checkpoints, local verification, durable memory,
  and pull-request publication; the durable workflow validates transitions and external waits
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

### Install the CLI (no checkout required)

Requires Git and [`uv`](https://docs.astral.sh/uv/getting-started/installation/).
The package requires Python 3.12 or newer; uv can provision a compatible Python.
This implementation is currently on `feat/todo-platform` in
[PR #15](https://github.com/Isaac12x/incident-resolver-agent/pull/15), so the `master`
README and installer do not include it until that PR is merged. Install the branch now:

```bash
curl -fsSL https://raw.githubusercontent.com/Isaac12x/incident-resolver-agent/feat/todo-platform/install.sh | \
  INCIDENT_HARNESS_SOURCE='git+https://github.com/Isaac12x/incident-resolver-agent.git@feat/todo-platform' sh
uv tool update-shell
```

Restart your shell after `uv tool update-shell` if `incident-agent` is not on `PATH`.
Alternatively, install directly with uv:

```bash
uv tool install --from 'git+https://github.com/Isaac12x/incident-resolver-agent.git@feat/todo-platform' incident-harness
```

After PR #15 is merged, the default-branch installer can be used:

```bash
curl -fsSL https://raw.githubusercontent.com/Isaac12x/incident-resolver-agent/master/install.sh | sh
```

### Configure and start

Run these from outside a repository containing `.agent` to use the per-user configuration:

```bash
incident-agent --help
incident-agent init
incident-agent config
```

Configure your model, repositories, and connectors in the TUI. Export the credentials named
by that configuration (`OPENAI_API_KEY` for the default hosted model,
`INCIDENT_AGENT_API_TOKEN` for control APIs, and `AGENT_WEBHOOK_SECRET` for alert intake), then:

```bash
incident-agent doctor
incident-agent run
```

`incident-agent update` upgrades the isolated uv tool installation, its dependencies, and managed
repository tools when repositories are configured. It retains the source selected at installation;
rerun the default-branch installer after merge to switch from the feature branch.
`incident-agent doctor` reports executable, credential, repository, and container
readiness. `run` installs missing managed tools when dependency installation is enabled, then
checks readiness before starting. The TUI Overview shows those checks.
The installer accepts `INCIDENT_HARNESS_SOURCE` to select a Git revision or fork.
`config` and `tui` open the same
editor; `run FILE.json` continues to submit a single incident. `run` without a file starts the
HTTP server and worker in the foreground.
Use the systemd deployment below for a background service that starts on boot.

Outside a checkout, configuration lives in `$XDG_CONFIG_HOME/incident-harness/config.toml`
(default `~/.config/incident-harness/config.toml`) and state in
`$XDG_STATE_HOME/incident-harness` (default `~/.local/state/incident-harness`). An existing
`.agent` directory, `INCIDENT_AGENT_CONFIG`, or `--config PATH` selects an explicit workspace.
Git worktrees remain isolated per incident; the state directory name is configurable.

New user configurations enable `server.require_api_auth`. Export `INCIDENT_AGENT_API_TOKEN`
for control API clients and `AGENT_WEBHOOK_SECRET` for signed alert intake before running.
API requests under `/mcp/*` and `/a2a/*` use `Authorization: Bearer TOKEN`; alert webhooks
use their existing HMAC signatures. Missing required credentials return HTTP 503 and incorrect
credentials return HTTP 401. Existing configs retain their previous authentication setting:
enable `server.require_api_auth = true` to migrate. Setting the API token also enforces bearer
authentication when the compatibility flag is false. Health and agent discovery stay public.
Credentials are environment values; configuration stores their variable names.

### Incident history and evaluations

Grafana intake stores events, grouping keys, fingerprints, duplicate references, and task links
in the runtime SQLite database. Resolved alerts are logged without starting a repair. Incident
history retains investigation root causes. New tasks refresh intelligence from changed history
and attach summary, predicted causes, and related incidents to the agent context.

The authenticated APIs include:

| Endpoint | Behavior |
| --- | --- |
| `GET /mcp/resources/intelligence/events` | Filter recent intake by `source`, `group_key`, and bounded `limit` |
| `GET /mcp/resources/tasks/{task_id}/summary` | Investigation/fix artifact summary, with explicit extractive fallback before artifacts exist |
| `POST /mcp/tools/rebuild_intelligence` | Train from labeled history and rebuild optional vector search |
| `POST /mcp/tools/predict_root_cause` | Rank learned causes for a JSON `text` field |
| `POST /mcp/tools/search_similar_incidents` | Search using JSON `query` and optional `limit` |

Root-cause prediction uses bounded logistic regression and needs at least two distinct labels.
Scores are experimental outputs, not calibrated confidence. FAISS search uses sentence-transformers
and the optional `intelligence` extra. Automatic enrichment uses cached model weights; explicit
rebuild can download weights. Dependency/model absence is reported with a lexical fallback.

Task summaries reuse the model-generated investigation and fix artifacts produced by the durable
workflow and the evidence recorded by verification. The API prefers existing `artifacts/local/fix.txt`
and `investigation.md` artifacts, preserving their Markdown and reporting method `agent-artifact`
with the source path. Before those artifacts exist, it returns an explicitly labelled
`extractive-fallback` assembled from the incident summary and description. HumanLayer’s
[`show-me`](https://github.com/humanlayer/skills/blob/main/plugins/show-me/skills/show-me/SKILL.md)
skill guides the concise, evidence-based explanation format for summaries, investigation explanations, and pull-request bodies.

```bash
incident-agent eval
incident-agent eval --suite repair --output repair-report.json
incident-agent eval labeled-history.jsonl --suite root-cause --output root-cause-report.json
incident-agent eval retrieval-cases.jsonl --suite retrieval --output retrieval-report.json
```

Contract evaluations cover intake and authorization. The repair evaluation exercises the actual
workflow in a temporary Git repository: an independent regression must fail before the fix and
pass through the lifecycle test gate and after local publication. Its bundled repair backend is
scripted, so this verifies the harness rather than model repair quality. Root-cause evaluation uses
a chronological holdout; retrieval reports recall against explicit query/relevance pairs and the
actual search method. Production quality and cost require representative data and a real model.

### Runtime policies and versions

Task state, event order, worker leases, and workspace identity are stored in `tasks.sqlite3`.
Task folders contain readable artifacts and can be reconstructed from the catalog; their location
is no longer the scheduling authority. Existing task folders migrate on startup.
`logs/runtime.jsonl` contains rotating structured operation records. Authenticated `/metrics`
exports persisted counts, failures, and elapsed time; `/mcp/resources/metrics` returns JSON.

Each invocation records prompt, skill, and connection hashes. Build and activate immutable bundles:

```bash
incident-agent bundle build
incident-agent bundle list
incident-agent bundle activate VERSION
incident-agent bundle rollback
```

A bundle captures config, prompt, connections, skill contents, and environment-variable references.
Restart the service after activation or rollback to apply all inputs. Keep credentials in the
environment. Bundle hashes detect content changes; they are not signatures from a trusted publisher.

The contextual UCB bandit learns tool success/failure rewards across runs. Use explicit tools or
compatible candidate sets. Retries are bounded and only enabled for designated read-only tools.
`agent.tool_registry` can point to an operator-managed JSON catalog of wheel artifacts pinned by
SHA-256. Agents can inspect the catalog and install approved entries when dependency installation
is enabled; installed callable extensions survive restart. MCP remains supported. See
[the extension contract](docs/tool-registry.md) for the schema and trust boundary.

Set `execution.mode = "container"` and a pre-pulled `execution.image` to confine repository shell
and lifecycle test commands. Containers mount only the worktree, drop capabilities, restrict
resources, and disable networking by default. Missing Docker/image prerequisites fail readiness.
The TUI Runtime tab exposes these controls. The default `host` mode retains existing behavior.
This boundary does not sandbox native subscription CLI tools, operator-trusted plugins, or MCP
servers; choose and configure those executors according to their own trust model.

### Development checkout

The harness requires Python 3.12 or newer. Install [`uv`](https://docs.astral.sh/uv/getting-started/installation/),
then run these commands from the repository checkout:

```bash
cp .env.example .env
uv sync
uv run incident-agent init
uv run incident-agent tui
```

Set `OPENAI_API_KEY` in `.env` when using the default hosted model. For a local
OpenAI-compatible model, leave it blank and select `local` in the TUI. In the TUI,
configure the model, repositories, connectors, and GitHub settings required by your
deployment. Secrets are read from environment variables whose names are stored in the
configuration; secret values are not written to `.agent/config.toml`.

Start the HTTP server and durable worker together with:

```bash
uv run incident-agent serve
```

The default listener is `http://127.0.0.1:8765`. In another terminal, check readiness:

```bash
uv run incident-agent healthcheck
```

To submit an incident directly from a JSON file, use:

```bash
uv run incident-agent run tests/fixtures/incident.json
```

For development, run the HTTP server without its embedded worker and start the worker
separately:

```bash
uv run incident-agent serve --no-worker
uv run incident-agent worker
```

The committed `.seed/specs/runtime.tree` is the source of truth for the untracked runtime layout.
`init` creates or repairs the complete `.agent/` skeleton through `seed-cli`. Runtime commands also
run the same bootstrap automatically when `.agent/` is absent, so a fresh checkout is ready on its
first invocation without committing local configuration, sessions, logs, repositories, or tasks.

The runtime loads `.env` from the current working directory without overriding variables already
exported by the shell. The fixed variables are documented in `.env.example`; custom model and MCP
connector credential names come from `.agent/config.toml`.

The installation also includes the repository-intelligence tools `code-review-graph`,
and `seed-cli`. Build both local code graphs for a checkout, or capture a structured filesystem
tree, with:

```bash
uv run incident-agent index /path/to/repository
uv run incident-agent tree --out structure.seed /path/to/repository
```

Graph output stays local in `.code-review-graph/` and `harness-out/`; `tree` delegates tree
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

New configurations use `gpt-6-astra` with `max` reasoning, the quality-first choice from
the [OpenAI model catalog](https://developers.openai.com/api/docs/models/gpt-6-astra)
checked on 2026-09-07. The SDK lead and its research/implementation agents share those
settings. The subscription CLI receives the selected model and reasoning on every fresh
and resumed incident run. Model access must be available through the configured account;
the harness does not retry with a cheaper model when that selection fails.

Saved OpenAI flagship configurations upgrade automatically, including `gpt-5/high` to
`gpt-6-astra/max`. The shipped `src/model-policy.json` lists the reviewed predecessors and target;
future releases update this policy after checking model and runtime support. A running worker
reads configuration and policy on every poll and applies changes to its next model invocation,
including resumed subscription sessions. Active calls finish normally; no manual restart is needed
for model settings or policy updates. Invalid updates retain the last working configuration.
Set `model.auto_upgrade = false` to pin a selection. Custom models, local servers and compatible
endpoints are preserved. Sampling, budgets, and session memory remain configurable in the TUI.

The resolver first fixes and verifies the incident, then expands through graph neighbors in
concentric rings. Set each repository's `responsibility_paths` to the owned directories/files
(default `["."]`, the configured repository). `verification_plan(seed_paths)` starts from the
incident files; `verification_plan([])` resumes the next unfinished ring. After graph neighbors,
the final ring checks remaining files in that area for relationships the graph may have missed.
Related defects receive repairs and regression checks within the existing task budget.

`run_tests(command, paths)` records the files actually verified in
`artifacts/local/verification-graph.json` under the task directory. Its `nodes` and `edges` use
code-review-graph fields, with repository-relative qualified names so worktree paths are stable.
Each run retains its command, result and SHA-256 input hashes. Successful matching commands are
reused across task resumes; source, dependency, test, fixture and configuration changes invalidate
affected evidence. Missing/stale graphs fall back to hashing all repository inputs. Unscoped
commands hash the whole checkout and do not claim coverage for individual paths. Use `force=true`
for changing external state or deliberate reproduction. Publication requires a completed area
plan and current passing checks; deployment verification still runs against the exact PR SHA.

Incident investigation establishes expected behavior from repository contracts, callers,
tests, and history before proposing a repair. Coding and testing skills require the fix to
restore that behavior, preserve intentional failures, and verify an actual regression.
The bundled [Ponytail adaptation](skills/ponytail/SKILL.md) runs before implementation and
review changes: reuse existing code, standard libraries, and platform features before adding
code. The simplify-and-verify loop lives in skills and uses the existing lifecycle tools and
retry budget. It adds no model router, dependency, or second orchestration loop.

The Ponytail adaptation includes its upstream [MIT notice](skills/ponytail/LICENSE).
Skills are shipped with the wheel and work offline. Repository-specific policies belong in
the configured project instructions and skill directories; examples use placeholder projects
and endpoints. Run `uv run pytest` and `uv run ruff check src tests` before contributing.

### Open Code Review before Playwright

Run `incident-agent tui`, open **Code review**, enable the integration, and enter the
model protocol, endpoint, model name, API-key environment variable name, and timeout.
**Save configuration and install / test OCR** persists those settings before installing
[`@alibaba-group/open-code-review`](https://github.com/alibaba/open-code-review) with npm
and running `ocr llm test`. Git 2.41 or newer and npm are required. The API key is read
from the environment; its value is never saved in the harness configuration.

Setup uses an existing `ocr` on PATH or installs it under `<runtime_root>/tools/ocr`.
For systemd, run this setup in the installed directory as the service user and supply
the named credential variable to both the setup session and the worker service.
Copying `config.toml` alone does not transfer the OCR executable or credentials.
Existing configurations keep OCR disabled until explicitly enabled in the TUI.

After a fix, the harness commits outstanding changes and runs:

```bash
ocr review --from main --to feature-branch --format json --output scan-result.json
```

The actual refs use the repository's configured base and incident branch; a remote-only
base uses `origin/<base>`. Output goes into the task filetree at
`artifacts/code-review/scan-result.json`, with an additional `<commit-sha>.json` report.
The current report enters the fix agent's context and lifecycle tool feedback. Findings
return the task to implementation via the reproduction state, within the task retry budget.
After repairs, local checks and publication run again. Only a complete report with no
findings can pass the gate; missing tools, credentials, invalid JSON, partial reviews,
warnings, and timeouts block the task. No live OCR calls occur when it is disabled.

The gate runs before publication and before configured Playwright lifecycle commands,
and is checked again against the current PR commit before deployment verification.
An unchanged reviewed commit reuses its passing result. Agents must use `run_tests`
for Playwright so execution goes through the lifecycle gate.

### systemd deployment

The CentOS/RHEL installer deploys the single service and an optional split HTTP/worker target:

```bash
sudo deploy/systemd/install-centos.sh
```

The installer copies the checkout's non-secret `.agent/config.toml` into a fresh deployment when
present, otherwise it writes runnable defaults. Configured local repositories are staged as bare
Git mirrors under `/opt/incident-harness/.agent/repositories`, and their `local_path` settings are
rewritten to those deployed mirrors. Existing deployed repositories are preserved during upgrades;
untracked checkout files and other runtime state are not copied. The installer then enables the
combined HTTP/worker service and waits for the readiness check to pass. To edit the installed
configuration later, run:

```bash
sudo runuser -u incident-harness -w /opt/incident-harness -- \
  /opt/incident-harness/.venv/bin/incident-agent tui
```

Model changes reload automatically. Restart the service after changes to listener, connector,
credential environment or other startup settings: `sudo systemctl restart incident-harness.service`.

The default intake listener is `0.0.0.0:8765`; only the HTTP process listens on that port. Set
`server.host`, `server.port`, and `server.public_url` in the TUI when a reverse proxy or a different
bind address is required.

The installer copies `deploy/systemd/incident-harness.env.example` to
`/etc/incident-harness/environment`. Put API keys, webhook secrets, connector tokens, and other
values there using the environment-variable names configured in the TUI. On every start,
`export-systemd-env` selects only the variables referenced by the current configuration and writes
the systemd environment file under `/run`.

To run with a host-authenticated subscription such as Codex:

1. Install the configured CLI (`codex` by default) so it is available on the systemd service
   user's `PATH`.
2. In the TUI Model tab, select `subscription-cli` and leave the command as `codex` (the harness
   adds `--yolo` automatically), or enter another compatible CLI command.
3. Authenticate as the service user before starting the service:

   ```bash
   sudo -u incident-harness -H codex login
   ```

   The units set `HOME=/var/lib/incident-harness` and `CODEX_HOME=/var/lib/incident-harness/.codex`,
   so the service uses that user's OAuth/configuration rather than an administrator's login. Do
   not put subscription OAuth state in `/etc/incident-harness/environment`; it is managed by the
   CLI. Another subscription CLI should use its own login command and state directory if it does
   not honor `CODEX_HOME`.

Restart the service after changing TUI configuration or authentication:

```bash
sudo systemctl restart incident-harness.service
sudo systemctl status incident-harness.service
```

The combined service and optional split target conflict at the systemd level and cannot run at the
same time. To install and start the split HTTP/worker topology instead, use:

```bash
sudo env INCIDENT_HARNESS_LAYOUT=split deploy/systemd/install-centos.sh
```

### GitHub login, service credentials, and PR publishing

Install GitHub CLI (`gh`) alongside Git before running the installer. In the TUI's Repositories
tab, **Log in to GitHub and load repositories**, select the repository, then **Clone/pull and
index** and save configuration. This login configures Git's HTTPS credential helper too.
The Runtime tab's **Agent GitHub login** is a review-filter identity, not an authentication field.

The installer seeds the selected checkout into `/opt/incident-harness/.agent/repositories`,
resolves GitHub repository names without regard to letter case, and saves the deployed `local_path`.
On upgrades it preserves `/opt/incident-harness/.agent/config.toml`; edit the installed TUI to
change the service's selection. Changes to a separate development checkout's TUI do not overwrite
an existing deployed configuration.

On first setup, the installer provisions the setup account's authenticated GitHub CLI token for
`incident-harness` through stdin. The service account keeps its own protected CLI credential store
under `/var/lib/incident-harness/.config/gh`. Existing service logins are preserved on upgrades.
Run the installer as the account used for setup (root in the documented installation); if `sudo`
changes accounts, authenticate the service account first instead:

```bash
cd /opt/incident-harness
sudo runuser -u incident-harness -- gh auth login --hostname github.com --git-protocol https --web
sudo runuser -u incident-harness -- gh auth setup-git --hostname github.com
```

Both Git operations and the built-in PR publisher use this account. The installer rewrites
GitHub SSH URLs to HTTPS for the service so old repository selections also use these credentials.
The account needs repository read/write and pull-request creation access. PRs are authored by the
authenticated account; changing the TUI's agent login does not change their author.
For token rotation, `GH_TOKEN` (or `GITHUB_TOKEN`) in `/etc/incident-harness/environment` overrides
the stored login after a restart. These values are exported to systemd; never put them in
`config.toml`. See [GitHub CLI authentication](https://cli.github.com/manual/gh_auth_login) and
[credential precedence](https://cli.github.com/manual/gh_help_environment).

For the subscription runtime, install the complete Codex CLI bundle somewhere the service can
execute it. A standalone release with `codex-code-mode-host` needs that matching helper beside
`codex`; copying only the main executable can allow login while leaving every agent run broken.
Use **Test subscription CLI** in the installed TUI to verify an actual structured response.
The harness converts its checkpoint models to the strict output schema required by Codex.

For a GitHub selection, `publish_mode = "auto"` now publishes to GitHub and reports authentication
errors rather than silently completing locally. `publish_mode = "github"` explicitly requires
GitHub publication; `local` remains an explicit local-only option. After successful local checks,
the harness commits the fix, pushes `incident-harness/fix/FIX-NAME` (a summary slug with a unique
incident suffix), and opens a PR against the selected base branch. Draft status follows the TUI.
The PR includes investigation evidence and local verification, and waits for verification of its
exact preview SHA. A retry reuses an existing open PR for the same branch.

Check the installed account and repository access from the service's working directory:

```bash
cd /opt/incident-harness
sudo runuser -u incident-harness -- gh auth status --hostname github.com
sudo runuser -u incident-harness -- gh repo view OWNER/REPOSITORY --json nameWithOwner,viewerPermission
sudo runuser -u incident-harness -- git ls-remote git@github.com:OWNER/REPOSITORY.git HEAD
sudo systemctl restart incident-harness.service
```

If you deliberately configure SSH instead of the installer's HTTPS mapping, the service needs
its own authorized SSH key and verified host entries in
`/var/lib/incident-harness/.ssh/known_hosts` (owned by `incident-harness`, mode `0600`).
Use the entries from [GitHub's published SSH host keys](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints),
or compare scanned fingerprints against that page before installing them. Do not disable host
verification. An SSH deploy key authenticates Git; the GitHub CLI still needs its login to open PRs.

`/health` performs live connection checks, in parallel with a five-second deadline per dependency.
It returns HTTP 503 and per-connection errors when Loki tenant queries, Grafana's authenticated
Loki datasource/proxy, or GitHub repository/PR access and Git base-branch reads fail. GitHub checks
also require repository push permission; they do not create a PR or prove every token write scope.
The worker status remains separate. An incoming Grafana webhook without a log-query connector
is reported as missing observability, rather than healthy. Outages after startup and recovery are
checked on subsequent requests. Failed/blocked tasks retain their original events and do not
restart automatically when authentication is repaired or the service restarts.
Concurrent incidents wait for a busy repository lock without consuming their failure budget.

The same tab selects the execution runtime. `agents-sdk` uses the configured API endpoint and keeps
the task and sub-agent histories in `.agent/sessions.sqlite3`. `subscription-cli` starts `codex --yolo
exec` by default and reuses device OAuth already completed by the host CLI. It captures the CLI thread ID,
uses `codex --yolo exec resume` after external deployment or review events, translates eligible configured
MCP servers into CLI configuration, and exposes authenticated per-run lifecycle commands. Change
`model.subscription_command` when using another Codex-compatible subscription CLI.

Long SDK sessions compact older items after `model.compaction_threshold` while retaining the newest
`model.session_history_limit` items. The extractive checkpoint is appended to the task's `memory.md`,
which is included together with global and repository memory on every resume. Set
`model.compaction_enabled = false` only when the selected provider handles context compaction itself.

The Safety tab also contains the complete system prompt. That prompt and the positive goals,
negative goals, guardrails, and safeguards are assembled into every investigation, implementation,
and review agent run as a binding instruction contract.

### Read-only production log connections

Incoming webhooks supply alert metadata; they do not provide a log-query client. Add native
`loki` and `grafana` connections in the TUI Connections tab, or configure them in
`.agent/config.toml`. Keep the existing Grafana webhook and give its query connection a distinct
name, or use a `grafana` query connection with the same name to support both intake and queries.
Use the address reachable from the service host (including the published port):

```toml
[[connectors]]
name = "loki"
purpose = "observability"
type = "loki"
url = "http://127.0.0.1:3100"
tenant_id = "your-tenant"
capabilities = ["logs", "metrics"]

[[connectors]]
name = "grafana"
purpose = "incident"
type = "grafana"
url = "http://127.0.0.1:3200"
datasource_uid = "your-loki-datasource"
auth_token_env = "GRAFANA_SERVICE_ACCOUNT_TOKEN"
capabilities = ["logs", "metrics"]
```

Save a Grafana Viewer service-account token in `/etc/incident-harness/environment` under the
configured environment variable. The existing systemd exporter includes that referenced variable.
Use `auth_token_env` for Loki too if its gateway requires a bearer token. Omit `tenant_id` only for
single-tenant Loki. Grafana proxy queries use the tenant headers provisioned on its datasource.
See the [Loki HTTP API](https://grafana.com/docs/loki/latest/reference/loki-http-api/) and
[Grafana datasource API](https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/api-legacy/data_source/).

Both agent runtimes receive `<connection_name>_query_range` and `<connection_name>_labels` tools.
Range queries require explicit incident start/end timestamps and are capped at 200 log records,
1 MB of response data, and 20 seconds. Tools can only read from the configured endpoint; agents
cannot override its URL, credentials, or tenant. Health failures omit upstream response bodies.
Restart the service after configuration changes, check `/health`, then explicitly resume the
relevant blocked tasks while preserving their original evidence and session histories.

### Durable agent lifecycle

The lead agent receives four stateful harness tools:

- `mark_investigation_complete` stores root cause, evidence, proposed fix, and reproduction status.
- `run_tests` executes and records a real command; failures consume the task retry budget.
- `open_pr` publishes only from a successfully tested state, or updates the known PR head on review.
- `remember` writes task or repository memory that survives process restarts and compaction.

Research and implementation run as bounded sub-agents with stable child session IDs. The lead agent
owns lifecycle transitions and verifies delegated conclusions. Deployment webhooks and authorized
review comments resume the same lead session instead of creating phase-specific conversations.
Review comments are stored in the task state before the worker wakes, so a process restart cannot
drop an authorized request. `open_pr` pushes an updated review branch and calls the configured
GitHub adapter before waiting for a fresh deployment.

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
guardrails, and safeguards from the same TUI. The built-in GitHub publisher uses the TUI GitHub CLI login as described above.
Optional GitHub MCP connectors authenticate separately through their configured token references.

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

Fresh configurations include a `grafana` webhook connector. Point the Grafana contact point at
`http://HOST:8765/hooks/incidents/grafana`. Normalized incident JSON is accepted as before. Native
Grafana Alerting payloads are also accepted when alert labels contain `repository` (or `repo`) and
optionally `environment` (or `env`); absent environment labels default to `production`.
When exactly one repository is configured, it is also used as the repository fallback for Grafana
alerts, so an existing alert group does not need to duplicate that label.
Grafana resolution notifications are acknowledged without creating a new incident task.
When `AGENT_WEBHOOK_SECRET` is set, configure the same secret in Grafana's HMAC signature settings
and use its default `X-Grafana-Alerting-Signature` header. The existing
`X-Agent-Signature-256: sha256=...` contract remains supported for other producers.

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

The implementation uses one asyncio event loop, small responsibility-based modules, SQLite task
state and session history, and no additional workflow framework. The workflow owns transition
validation, retries, deployment events, and recovery. Each task's durable lead session decides when
to investigate, delegate, edit, test, remember, and publish by calling the workflow's lifecycle tools.


## Known limitations, pitfalls and non-goals

See [TODO.md](TODO.md) for the request-by-request acceptance matrix and remaining external
validation needs. The online tool policy learns execution success, not end-to-end repair quality.
A production benchmark needs representative labeled incidents and a selected model. Host executors,
trusted extensions, and native subscription CLI tools require their own isolation configuration.

Repository knowledge graphs reduce unnecessary source loading. Built-in and third-party skills
continue to provide the incident lifecycle instructions.

## LICENSE

See the LICENSE file at the root of this repository for more information.
