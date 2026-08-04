# Changelog

## Unreleased

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
  clone or fast-forward managed checkouts and generate graphify and code-review-graph indexes.
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
- Install graphify, code-review-graph, and seed-cli with the harness; add graph indexing and
  seed-backed structured-tree CLI commands.
- Add the complete minimal regression matrix with over 90% coverage in every source file.
