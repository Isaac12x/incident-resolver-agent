---
title: Configuration reference
---

# Configuration reference

[Documentation home](index.md) · [Example configuration](configuration-example.toml)

This reference follows `src/config.py` on the default branch. Defaults below are schema defaults; first-run initialization can choose different values as described in [Getting started](index.md#choose-a-configuration-file). Omit optional values to leave them unset: TOML has no `null` literal.

Top-level scalar options must appear before any TOML section header. Each `[section]` is a table; repeat `[[repositories]]` and `[[connectors]]` for multiple entries. Unrecognized keys are currently ignored by the configuration models, so check spelling carefully.

## Sections

- [root](#root)
- [model](#model)
- [triage](#triage)
- [trigger](#trigger)
- [agent](#agent)
- [safety](#safety)
- [github](#github)
- [server](#server)
- [deployment](#deployment)
- [code_review](#code_review)
- [permissions](#permissions)
- [execution](#execution)
- [repositories](#repositories)
- [repositories.playwright](#repositoriesplaywright)
- [connectors](#connectors)

## root

Set these at the top of the file.

| Option | Type / accepted values | Default | Meaning |
| --- | --- | --- | --- |
| `runtime_root` | string | `".agent"` | Directory for durable runtime state, task artifacts, worktrees, logs, and bundles. Relative paths are resolved from the process working directory. |
| `max_concurrent_tasks` | integer | `2` | Maximum number of incident tasks processed concurrently. Bounds: ≥ 1. |
| `poll_interval_seconds` | number | `2.0` | Worker polling interval in seconds. Bounds: > 0. |

## model

TOML header: `[model]`.

| Option | Type / accepted values | Default | Meaning |
| --- | --- | --- | --- |
| `auto_upgrade` | boolean | `true` | Apply the shipped model upgrade policy on configuration load. Only matching OpenAI models without a custom endpoint and outside local mode are upgraded; set false to pin the model and reasoning. |
| `runtime` | `agents-sdk`, `subscription-cli` | `"agents-sdk"` | Execution backend: agents-sdk uses the API; subscription-cli uses a host-authenticated Codex CLI. |
| `mode` | `local`, `remote` | `"remote"` | Select remote or local model hosting. Local agents-sdk mode requires base_url. |
| `provider` | string | `"openai"` | Provider label used by the upgrade policy. Changing this alone does not select a different SDK protocol; use base_url for an OpenAI-compatible endpoint. |
| `base_url` | string or null | Unset | Optional OpenAI-compatible endpoint. A custom endpoint selects the SDK chat-completions client. |
| `api_key_env` | string | `"OPENAI_API_KEY"` | Environment variable containing the model API key. Local endpoints can run without a real key. Subscription CLI authentication is managed by the CLI. |
| `organization_env` | string or null | Unset | Optional environment variable containing an OpenAI organization identifier. |
| `name` | string | `"gpt-6-astra"` | Model identifier. Whitespace is trimmed and blank names are rejected. Bounds: minimum length 1. |
| `reasoning` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` or null | `"max"` | Requested reasoning effort; actual support depends on the model and backend. |
| `temperature` | number or null | Unset | Optional sampling temperature for the SDK backend. Bounds: ≥ 0, ≤ 2. |
| `top_p` | number or null | Unset | Optional nucleus-sampling probability for the SDK backend. Bounds: ≤ 1, > 0. |
| `max_tokens` | integer or null | Unset | Optional SDK output token limit. Bounds: ≥ 1. |
| `parallel_tool_calls` | boolean | `false` | Allow the SDK model to request parallel tool calls. |
| `show_execution_details` | boolean | `true` | Print sanitized live agent progress and tool activity. |
| `max_turns_per_iteration` | integer | `30` | Maximum SDK agent turns within one iteration. Bounds: ≥ 1. |
| `max_task_iterations` | integer | `8` | Retry/iteration budget used by the durable workflow before blocking exhausted work. Bounds: ≥ 1. |
| `tool_timeout_seconds` | integer | `600` | Timeout budget for tool execution and subscription CLI operations, in seconds. Bounds: ≥ 1. |
| `session_history_limit` | integer | `60` | Number of recent session items retained during compaction; must be lower than compaction_threshold. Bounds: ≥ 10. |
| `compaction_enabled` | boolean | `true` | Enable automatic session-history compaction. |
| `compaction_threshold` | integer | `120` | Session item count that triggers compaction; must exceed session_history_limit. Bounds: ≥ 20. |
| `subscription_command` | array of string | `["codex", "--yolo"]` | Argument array used to launch Codex. Must be nonempty for subscription-cli. The default --yolo runs without interactive approvals. |
| `subscription_profile` | string or null | Unset | Optional Codex profile passed through --profile. |

## triage

TOML header: `[triage]`.

| Option | Type / accepted values | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | boolean | `false` | Enable optional TypeSafe incident assessment before investigation. |
| `mode` | `shadow`, `enforce` | `"shadow"` | shadow records the recommendation while continuing to the agent; enforce can route sufficiently supported non-code incidents to operator review. |
| `model` | string | `"jev-1.13.0"` | TypeSafe model identifier; must be nonempty. Bounds: minimum length 1. |
| `api_key_env` | string | `"TYPESAFE_API_KEY"` | Environment variable containing the TypeSafe key; must be a valid variable name. |
| `timeout_seconds` | number | `10.0` | Total assessment time budget in seconds, including bounded retries. Bounds: ≤ 60, > 0. |
| `review_threshold` | number | `0.95` | Evidence/confidence threshold for operator-review routing. The code-fix score must also be at most 1 minus this threshold. Missing credentials, failures, or invalid answers fall back to agent investigation. Bounds: ≤ 1, > 0.5. |

## trigger

TOML header: `[trigger]`.

| Option | Type / accepted values | Default | Meaning |
| --- | --- | --- | --- |
| `mode` | `hook`, `workflow`, `agent-call` | `"hook"` | Saved intake preference; currently does not disable or select server endpoints. |
| `hook_path` | string | `"/hooks/incidents"` | Incident webhook route prefix. Requests use this prefix followed by /{connector}. Must start with /, be nonempty after trailing slashes are stripped, and contain no braces, question mark, or #. |
| `workflow_name` | string | `""` | Saved workflow name; currently not used to launch a workflow. |
| `agent_name` | string | `"incident-agent"` | Saved agent name; currently not used to route intake or change discovery identity. |
| `require_ack` | boolean | `false` | Saved acknowledgment preference; currently does not gate incident processing. |

## agent

TOML header: `[agent]`.

| Option | Type / accepted values | Default | Meaning |
| --- | --- | --- | --- |
| `tool_registry` | string or null | Unset | Optional path to an operator-managed JSON catalog of digest-pinned tool wheels. See the extension contract linked below. |
| `max_tool_retries` | integer | `2` | Retry budget for eligible tools; not permission to retry every mutating operation. Bounds: ≥ 0, ≤ 5. |
| `system_prompt` | string | See shipped defaults below | Lead-agent operating instructions and structured output contract. Must not be blank. The full shipped default is listed below. |
| `skill_directories` | array of string | `["skills", ".agents/skills", ".claude/skills", ".codex/skills"]` | Repository-relative directories searched for skills; absolute paths, blank entries, and .. components are rejected. |
| `max_auto_skills` | integer | `8` | Maximum automatically selected skills. Bounds: ≥ 0, ≤ 32. |
| `max_subagents` | integer | `2` | Maximum configured subagents; zero disables delegation. Bounds: ≥ 0, ≤ 8. |

## safety

TOML header: `[safety]`.

| Option | Type / accepted values | Default | Meaning |
| --- | --- | --- | --- |
| `positive_goals` | array of string | See shipped defaults below | Desired outcomes added to agent instructions. Replacing this array replaces the shipped goals. |
| `negative_goals` | array of string | See shipped defaults below | Outcomes the agent is instructed to avoid. Replacing this array replaces the shipped prohibitions. |
| `guardrails` | array of string | See shipped defaults below | Instruction-level operating boundaries. These complement permissions and runtime validation. |
| `safeguards` | array of string | See shipped defaults below | Instruction-level evidence, verification, and publication checks. These complement deterministic lifecycle gates. |

## github

TOML header: `[github]`.

| Option | Type / accepted values | Default | Meaning |
| --- | --- | --- | --- |
| `webhook_secret_env` | string | `"GITHUB_WEBHOOK_SECRET"` | Environment variable containing the GitHub webhook HMAC secret. |
| `draft_pull_requests` | boolean | `true` | Create incident pull requests as drafts. |
| `agent_login` | string | `"incident-agent[bot]"` | Bot login used to avoid responding to the agent’s own comments. |
| `agent_mention` | string | `"@incident-agent"` | Mention string used for review routing. |
| `allowed_author_associations` | array of string | `["OWNER", "MEMBER", "COLLABORATOR"]` | GitHub author associations authorized to request review changes. |
| `conflict_poll_interval_seconds` | number | `300` | Delay between mergeability checks while an open pull request has a conflict. Bounds: > 0. |

## server

TOML header: `[server]`.

| Option | Type / accepted values | Default | Meaning |
| --- | --- | --- | --- |
| `host` | string | `"0.0.0.0"` | HTTP bind address; 0.0.0.0 listens on all IPv4 interfaces. |
| `port` | integer | `8765` | HTTP listening port. Bounds: ≥ 1, ≤ 65535. |
| `public_url` | string or null | Unset | Optional externally reachable service URL used in A2A discovery and service URL helpers. |
| `webhook_secret_env` | string | `"AGENT_WEBHOOK_SECRET"` | Environment variable containing the incident webhook HMAC secret. |
| `api_token_env` | string | `"INCIDENT_AGENT_API_TOKEN"` | Environment variable containing the bearer token for control APIs. |
| `require_api_auth` | boolean | `false` | Require configured bearer authentication on protected APIs. Even when false, setting the token enforces authentication. Missing required tokens produce 503; invalid tokens produce 401. Health and discovery remain public. |

## deployment

TOML header: `[deployment]`.

| Option | Type / accepted values | Default | Meaning |
| --- | --- | --- | --- |
| `reachability_timeout_seconds` | integer | `120` | Time budget to wait for the preview URL to become reachable. Bounds: ≥ 1. |
| `poll_interval_seconds` | number | `2.0` | Interval between preview reachability checks; separate from worker polling. Bounds: > 0. |

## code_review

TOML header: `[code_review]`.

| Option | Type / accepted values | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | boolean | `false` | Run the optional Open Code Review gate before publication and preview verification. |
| `protocol` | `openai`, `openai-responses`, `anthropic` | `"openai"` | Protocol used by Open Code Review: openai, openai-responses, or anthropic. |
| `base_url` | string | `"https://api.openai.com/v1"` | Review service endpoint. When enabled, must be an HTTP(S) URL with a host and without credentials, query, or fragment. |
| `model` | string | `""` | Review model identifier; required and nonblank when enabled. |
| `api_key_env` | string | `"OPENAI_API_KEY"` | Environment variable containing the review API key; required and nonblank when enabled. |
| `timeout_seconds` | integer | `600` | Open Code Review command timeout in seconds. Bounds: ≥ 1. |

## permissions

TOML header: `[permissions]`.

| Option | Type / accepted values | Default | Meaning |
| --- | --- | --- | --- |
| `mode` | `read-only`, `workspace` | `"workspace"` | Workspace mutation policy: read-only or workspace. |
| `allow_dependency_installation` | boolean | `true` | Allow managed dependency/tool installation, including approved tool extensions. |
| `allow_migrations` | boolean | `false` | Permit migration changes within the workspace policy. |
| `allow_ci_modification` | boolean | `false` | Permit CI configuration changes within the workspace policy. |
| `allow_snapshot_updates` | boolean | `false` | Permit snapshot changes within the workspace policy. |
| `allow_review_resolution` | boolean | `true` | Permit resolving authorized review threads. |

## execution

TOML header: `[execution]`.

| Option | Type / accepted values | Default | Meaning |
| --- | --- | --- | --- |
| `mode` | `host`, `container` | `"host"` | Run repository shell and lifecycle test commands on the host or in Docker containers. |
| `image` | string | `"python:3.12-slim"` | Container image reference; must be nonempty, contain no whitespace, and not start with a dash. Pre-pull the image before use. |
| `network` | boolean | `false` | Enable networking for container commands. Has no isolation effect in host mode. |
| `memory_mb` | integer | `512` | Container memory limit in MiB. Bounds: ≥ 64, ≤ 32768. |
| `pids_limit` | integer | `128` | Container process-count limit. Bounds: ≥ 16, ≤ 4096. |

## repositories

TOML header: `[[repositories]]`.

| Option | Type / accepted values | Default | Meaning |
| --- | --- | --- | --- |
| `name` | string | Required | Required repository identity, normally owner/repository. Configuration lookup is case-insensitive. |
| `clone_url` | string or null | Unset | Optional remote clone URL. |
| `local_path` | string or null | Unset | Optional local repository path. |
| `publish_mode` | `auto`, `github`, `local` | `"auto"` | github publishes a GitHub PR; local keeps publication local; auto falls back to local publication when the GitHub adapter is unavailable. |
| `base_branch` | string | `"main"` | Branch used as the incident baseline and PR base. Set this explicitly for repositories whose default branch is master. |
| `responsibility_paths` | array of string | `["."]` | Nonempty list of repository-relative paths the agent owns. Entries must be nonblank, with no absolute paths or .. components. |
| `incident_environments` | array of string | `["production"]` | Environments allowed to create incident tasks for this repository. |
| `verification_environment` | string | `"preview"` | Required deployment environment. Verification must also match the repository and exact current PR head SHA. |
| `project_instructions` | string | `"AGENTS.md"` | Repository instruction-file path loaded into agent context. |

## repositories.playwright

TOML header: `[repositories.playwright]`.

| Option | Type / accepted values | Default | Meaning |
| --- | --- | --- | --- |
| `command` | string | `""` | Command used to verify a preview deployment. Arguments are split without a shell; use a script for pipes or shell expansion. Configure a real test command; the empty default cannot complete required preview verification. |
| `base_url_env` | string | `"PLAYWRIGHT_TEST_BASE_URL"` | Environment variable set to the matched deployment URL for the test command. |
| `timeout_seconds` | integer | `900` | Timeout for each Playwright command attempt. Bounds: ≥ 1. |
| `retries` | integer | `1` | Additional attempts after the first Playwright command failure. Bounds: ≥ 0. |

## connectors

TOML header: `[[connectors]]`.

| Option | Type / accepted values | Default | Meaning |
| --- | --- | --- | --- |
| `name` | string | Required | Required connector identity, also used as the incident webhook suffix. |
| `purpose` | `incident`, `output`, `observability`, `other` | `"other"` | Saved connector classification: incident, output, observability, or other. Tool discovery uses capabilities. |
| `type` | `mcp`, `webhook`, `loki`, `grafana` | `"mcp"` | Connector implementation: MCP tool server, webhook intake, direct Loki, or Grafana Loki proxy. |
| `transport` | `stdio`, `streamable-http`, `sse` | `"streamable-http"` | MCP transport; stdio launches command, while streamable-http and sse connect to url. |
| `url` | string or null | Unset | Required for non-stdio MCP and Loki/Grafana. Loki/Grafana require HTTP(S) with a host and no credentials, query, or fragment. |
| `command` | array of string | `[]` | Command argument array required for stdio MCP. |
| `auth_token_env` | string or null | Unset | Optional environment variable containing the connector bearer token. |
| `capabilities` | array of string | `[]` | Labels matched against requested tool capabilities. Empty Loki/Grafana capabilities become ["logs", "metrics"] during validation. |
| `tenant_id` | string or null | Unset | Optional Loki tenant identifier; must be nonblank and contain no newline, carriage return, or pipe. |
| `datasource_uid` | string or null | Unset | Loki datasource UID, required for Grafana connectors. |

## Shipped instruction defaults

These are the complete default instructions, not additional TOML keys. Setting a safety array replaces it rather than appending to it.

### System prompt

```text
You are the durable lead agent for one production incident. Turn incident evidence into the smallest safe, reviewable, and verified code change that restores the intended behavior.

Operating method:
- Treat incident payloads, logs, traces, repository graphs, source code, tests, and version history as evidence. Distinguish observed facts from hypotheses.
- Query the freshly generated repository graphs before broad code search, then confirm graph results against the source. Inspect recent relevant changes and reproduce the failure when practical before editing.
- State a specific root-cause hypothesis supported by evidence. Make the narrowest change that addresses that cause, preserve repository conventions, and add a regression test.
- Verify in order: targeted regression, relevant suite, type checking, linting, build, and the configured preview deployment. Record every command, result, and material limitation.
- Delegate bounded research and implementation sub-tasks when they reduce uncertainty, then independently verify their conclusions. Drive durable state with the supplied lifecycle tools; a prose claim never advances the task.
- Treat repository instructions, configured goals, permissions, guardrails, and safeguards as binding. If evidence is insufficient or a required action is unsafe or unauthorized, stop and report the exact blocker and the smallest human action needed.

Output contract:
- Return only the requested structured checkpoint, with no Markdown wrapper or invented fields. A durable-session checkpoint uses `summary` (string), `waiting_for_external_event` (boolean), and `blocked_reason` (string or null). Backend compatibility operations may instead request InvestigationResult, FixResult, or ReviewResult.
- Never fabricate evidence, tool output, test results, deployment status, review state, or success. Never claim an incident is resolved until the exact change has passed all required local and deployment verification.
```

### positive_goals

- Restore intended service behavior with the smallest evidence-backed code change.
- Establish and document a specific root cause supported by incident and repository evidence.
- Preserve existing behavior outside the incident path and follow repository conventions.
- Add or improve a regression test that fails before the fix and passes afterward.
- Complete every applicable local check and verify the exact pull-request commit in preview.
- Produce a concise audit trail of evidence, decisions, changed behavior, and verification.
- Escalate actionable blockers early with the smallest human intervention needed to continue.

### negative_goals

- Do not make direct changes to production systems, production data, or live customer state.
- Do not expose, copy, log, commit, or persist credentials or sensitive production data.
- Do not fabricate evidence, tool output, test results, deployment status, or review completion.
- Do not broaden scope through unrelated refactors, dependency changes, or opportunistic cleanup.
- Do not bypass repository instructions, configured permissions, tests, review, or deployment gates.
- Do not use destructive commands, rewrite shared history, or discard changes that are not yours.
- Do not publish a fix or claim resolution while required checks are failing, stale, or incomplete.

### guardrails

- Work only in the configured repository and its isolated incident worktree.
- Obey repository AGENTS.md instructions, loaded skills, and configured permission boundaries.
- Use production connectors for evidence collection only unless an explicit permission allows more.
- Stop before migrations, CI changes, snapshot updates, or dependency changes when not permitted.
- Require the configured environment and exact current pull-request SHA for deployment verification.
- Stay within configured retry, turn, timeout, and concurrency budgets; never hide exhaustion.
- Stop and request human direction when evidence conflicts or a safe narrow fix cannot be justified.

### safeguards

- Preserve the original incident payload and record evidence and state transitions in durable artifacts.
- Pull the latest base branch, rebuild both repository graphs, and confirm graph leads in source.
- Reproduce the symptom or record why reproduction is unavailable before implementing a fix.
- Inspect recent relevant history and the final diff; reject unrelated or unexpectedly generated files.
- Run a targeted regression first, then the relevant suite, type checks, lint, and build when available.
- Inspect the staged diff for secrets and ensure graph indexes, credentials, and runtime files are excluded.
- Publish through a reviewable pull request and verify the exact deployed head SHA before completion.
- Fail closed and escalate with evidence when a required tool, permission, test, deployment, or review fails.

## Environment-only controls

These variables are read directly rather than stored as TOML options. Credential-variable names are configurable through the `_env` options above.

| Variable | Default when unset | Meaning |
| --- | --- | --- |
| `INCIDENT_AGENT_CONFIG` | Automatic discovery | Select a config file unless `--config` is supplied. |
| `XDG_CONFIG_HOME` | `~/.config` | Parent directory for the per-user `incident-harness/config.toml`. |
| `XDG_STATE_HOME` | `~/.local/state` | Parent directory used for new per-user runtime state. Existing `runtime_root` values are retained. |
| `INCIDENT_HARNESS_SOURCE` | Release wheel | Explicit Git revision, fork, local path, or wheel URL for installation and update. |
| `INCIDENT_HARNESS_REPOSITORY` | `Isaac12x/incident-resolver-agent` | GitHub repository whose release API supplies the wheel. |
| `INCIDENT_HARNESS_VERSION` | `latest` | GitHub release tag to install, such as `v0.2.0`. |
| `INCIDENT_HARNESS_RELEASE_ASSET` | First `incident_harness-*.whl` asset | Exact wheel asset name to select from the release. |
| `INCIDENT_HARNESS_PACKAGE` | `incident-harness` | Package name installed by `install.sh`. |
| `INTELLIGENCE_ENABLE_DOWNLOAD` | Unset | Any nonempty value allows automatic vector-index builds to download sentence-transformer weights; even `"0"` enables it. Unset it to require cached weights for automatic builds. Explicit rebuilds can allow downloads independently. Optional vector dependencies are still required. |

## Operational notes

- Restart the worker after editing configuration. If a versioned bundle is active, its captured configuration is used; rebuild and activate a bundle, or update your bundle selection, then restart.
- Container execution covers repository shell and lifecycle test commands. Native subscription CLI tools, trusted plugins, and MCP servers have their own execution boundaries.
- See the [tool extension contract](tool-registry.md) for registry structure and trust requirements.
- `repositories` and `connectors` default to empty arrays. A new per-user installation adds a Grafana webhook connector.
