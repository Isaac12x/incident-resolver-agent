# Changelog

## Unreleased

- Store harness task state, events, leases, sessions, incident history, metrics, and operation
  checkpoints in indexed SQLite tables with transactional updates. Import current JSON state
  once, prefer it over older SQLite sources, and retain readable artifacts and original sources.
- Declare and validate lifecycle transitions at the shared persistence boundary, retaining the
  agent reasoning loop and existing verification gates.
- Persist publication and deployment-verification attempts and outcomes across restarts, with
  bounded retries and input-specific recovery.

- Persist subscription CLI session identities before completion so interrupted first runs resume
  the same conversation. Periodically recover committed work and review requests, back off on
  occupied leases, and stop owned tasks and CLI processes on cancellation.

- Resolve duplicate case-insensitive repository path aliases during discovery, preserve genuine
  ambiguity errors, and skip the disabled code-review gate for compatibility workflows.
- Add optional Open Code Review setup in the TUI, saving model configuration before installation
  and connectivity testing. Review incident branches before publishing and Playwright, retain JSON
  reports in task artifacts, and return findings to the fix context with bounded retries.
- Correct the package license path so installations and builds use the existing `LICENSE` file.
- Clarify CLI installation with working pre-merge curl/uv commands, PATH setup, credentials,
  configuration locations, and the distinction between foreground operation and systemd deployment.

- Resolve bundled helper executables inside their Python environment, use the correct seed-cli
  package for managed installation/update, and reject unexpected nested bundle manifests.

- Complete runtime integrations for contextual tool selection, digest-pinned callable extensions,
  versioned configuration/skill bundles, and installed readiness/update commands.
- Enrich new incidents with history-based predictions, related incidents, and summaries derived from
  model-generated investigation/fix artifacts, with an explicit extractive fallback before artifacts
  exist. Extend the show-me guidance to incident summaries and investigation explanations.
- Add chronological holdout/retrieval evaluations and a repair evaluation through the real workflow.
- Move task state, ordered events, leases, and workspace identity into a durable SQLite catalog;
  retain task folders as recoverable artifacts and migrate existing state.
- Add rotating structured operation logs and authenticated persisted metrics, plus optional
  restricted container execution for repository shell and lifecycle test commands.
- Expose readiness and container execution policy in the configuration TUI.

- Add a curlable uv tool installer, update/config commands, foreground `run`, and persistent
  per-user configuration and state while retaining incident-file submission.
- Embed Grafana event logging, grouping and duplicate metadata, incident history, bounded
  logistic root-cause prediction, extractive summaries, and optional FAISS similarity search.
- Add offline contract evaluations with reproducible JSON reports and packaged datasets.
- Add control-API bearer authentication and fail-closed webhook configuration for new user
  installations; export API credentials to systemd through environment references.
- Record prompt, skill and connection content hashes; retry MCP discovery within a fixed budget
  and reject workspace directory replacement.
- Fix repository discovery on case-insensitive filesystems and the package license resource.

- Keep deployment verification outside the durable-agent routing path, persist authorized review
  comments across worker restarts, and explicitly push/update existing pull-request heads.
- Bridge runtime MCP adapters into the subscription CLI alongside static connector configuration;
  delegated implementation agents receive the same MCP servers as research agents.
- Configure systemd services with a service-user `HOME`/`CODEX_HOME` for subscription CLI OAuth,
  and document Codex authentication and runtime setup for systemd deployments.
- Run the Codex subscription backend with `codex --yolo` by default, including for legacy
  configurations that only specify `codex`.
- Replace phase-per-call execution with one resumable lead-agent session per task. Lifecycle tools
  now persist investigation, testing, memory, and PR transitions, while deployment/review events
  resume the same session and stable research/implementation sub-agent tree.
- Add bounded session compaction into task memory and load global, repository, and task memory on
  every resume.
- Add a selectable, host-authenticated subscription CLI backend (Codex by default) with thread
  resume, structured InvestigationResult/FixResult/ReviewResult/SessionResult parsing, native
  workspace/graph tools, MCP configuration mapping, and an authenticated lifecycle-tool bridge.
- Rewrite repository history to remove `codedb.snapshot` and `handoff.md`, and consolidate all
  commits older than three hours into one pre-cutoff commit.
- Discover nested built-in and repository-local `SKILL.md` files before every agent operation,
  automatically load contextual matches alongside required lifecycle skills, and record the
  preflight resolution in the task event log.
- Keep `.agent/` strictly untracked, commit a valid `.seed` runtime template, and add an idempotent
  `incident-agent init`/first-run bootstrap that recreates the complete runtime skeleton through
  `seed-cli`.
- Require operation-specific structured model output and accept the Agents SDK's validated Pydantic
  results, preventing successful local incident fixes from exhausting retries as invalid JSON.
- Show concise agent progress in the terminal by default: bounded reasoning summaries, sanitized
  tool names and targets, tool success/failure, and run status. Keep raw model JSON, complete SDK
  events, tool arguments, and tool output hidden, with a persisted TUI toggle for a quiet console.
- Add a current-incident conversation recall tool that searches durable SQLite messages through
  ripgrep and loads bounded matches into agent context when prior work or rationale is needed.
- Expand the default agent system prompt with a graph-first investigation and verification method,
  explicit structured-output schemas, honest blocker reporting, and stronger evidence standards;
  provide comprehensive positive goals, negative goals, guardrails, and safeguards by default.
- Escape newlines, carriage returns, and tabs when writing TOML so multiline system prompts remain
  valid and round-trip exactly through the configuration file.
- Fix overlapping Textual form sections so model, runtime, repository, connection, and safety
  controls remain labeled, focusable, scrollable, and editable from the keyboard.
- Add repository onboarding through GitHub CLI web login/repository selection or a clone URL;
  clone or fast-forward managed checkouts and generate code-review-graph indexes.
- Pull the latest base branch for every new incident, rebuild both graphs before investigation,
  preload graph query results into agent context, and expose follow-up graph search/impact tools.
- Connect configured stdio, Streamable HTTP, and SSE MCP servers at runtime, support bearer-token
  environment references, and add working connection tests to the TUI.
- Populate new configurations with operational system-prompt goals, negative goals, guardrails,
  and safeguards instead of empty safety fields.
- Require every incident-agent operation to make an initial tool call before reporting its result,
  while resetting later turns to automatic tool selection so the model can complete normally.
- Document only environment variables the runtime consumes, remove unused GitHub App credential
  settings, and load project `.env` files without overriding exported process variables.
- Upgrade to the current compatible OpenAI Agents SDK line, pass configured reasoning effort into
  model settings, pin the MCP dependency range required by that SDK, and require a patched FastMCP
  release for the repository graph tooling.
- Harden workspace tools by removing shell interpretation, blocking path/control-directory escapes,
  filtering credential environment variables, and enforcing configured write/install/migration/CI/
  snapshot permissions.
- Stop workflows from advancing when the agent reports failed local or review-change tests, and
  honor configured Playwright retry counts during deployment verification.
- Serialize per-task worker execution while replaying wake-ups that arrive during an active run.
- Recover stale process locks after crashes and sanitize incident identifiers before using them in
  Git branch names.
- Honor the configured incident webhook path and validate it as a safe absolute route.
- Enforce the vision's greater-than-90% per-file coverage requirement and add regression coverage
  for CLI dispatch, runtime safety boundaries, retry behavior, and workflow test gates.
- Add a resolvable skill manifest, trigger metadata, and an `AGENTS.md` dispatch table for all seven
  built-in incident workflow skills.
- Include the project description, README, and license in built package metadata.
- Render TUI validation failures as plain text so incomplete local-model drafts can be corrected
  without crashing the configuration screen.
- Fix model execution by passing the Agents SDK `ModelSettings` object, so configured model and
  runtime controls are accepted by the agent runtime instead of failing during execution.
- Fix the TUI's Add connection action so it can create an incomplete connector form without
  triggering HTTP URL validation before the user has entered connection details.
- Normalize built-in skills to `skills/{folderName}/SKILL.md` with frontmatter headers and update
  the agent loader and project manifests to use the nested layout.
- Replace the minimal Textual form with a complete tabbed configuration TUI covering local/remote
  OpenAI-compatible models, triggers, runtime, repositories, connectors, permissions, and safety
  goals. Custom base URLs and environment-variable credential references are supported without
  storing secret values.
- Add a persisted, editable system prompt and pass the full TUI safety contract into every agent
  operation.

## 0.1.0 - 2026-08-04

- Implement the durable incident task model, atomic filesystem storage, recovery, and locking.
- Add guarded coding tools, default OpenAI Agents execution, connector adapters, and persistent memory.
- Add GitHub webhook security and event routing plus strict preview deployment verification.
- Add HTTP, MCP-compatible, A2A, CLI, worker, and Textual configuration surfaces.
- Add local-only repository mode with conventional `.agent(s)/repositories` discovery, isolated
  branches, local commits, and durable local PR records when GitHub is unavailable.
- Install code-review-graph and seed-cli with the harness; add graph indexing and
  seed-backed structured-tree CLI commands.
- Add the complete minimal regression matrix with over 90% coverage in every source file.
