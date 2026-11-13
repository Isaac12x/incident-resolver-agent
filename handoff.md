
## Configuration documentation — 2026-09-18

Branch: docs/configuration-reference, based on origin/master (1597261).
Added a GitHub Pages/Jekyll documentation home, complete 99-option configuration reference,
example TOML, and Pages configuration. Linked the docs from README and updated CHANGELOG.
Validated all documented option names against Config.model_json_schema(), parsed and validated
example TOML through Config, checked new Markdown links/anchors, and ran git diff --check.
YAML/front matter parsing and Markdown rendering passed for all 99 TOML option rows.
Also documented six environment-only configuration/installer controls.
Full Jekyll build could not run: RubyGems DNS lookup timed out during dependency setup.
No live Pages deployment or settings change performed.
Original checkout's uncommitted work is preserved; changes live in the isolated worktree
/tmp/incident-harness-configuration-docs. No VISION.md exists on the source or default branch.

PR: https://github.com/Isaac12x/incident-resolver-agent/pull/20
Initial review check at approximately 2026-09-18 08:10 UTC: mergeable, no general or inline
comments, reviews, or CI checks.
Follow-up at 2026-09-18 08:15:44 UTC (over five minutes later): still mergeable, with no
general/inline comments, reviews, or CI checks. No review changes were requested.
Monitoring ends with this handoff; no persistent background monitor was installed.

## PR #23 integration repair — 2026-09-18

Worktree: `/tmp/incident-harness-pr23`; local branch `fix/pr23-integration`, targeting
`feat/sqlite-runtime-state` on PR https://github.com/Isaac12x/incident-resolver-agent/pull/23.
Merged `origin/master` (5fbbcd9) into the existing PR without rewriting history. Preserved
SQLite/application-scoped repair and master’s TypeSafe triage/configuration documentation.
No VISION.md exists. Original checkout changes remain untouched.

Resolved four conflicted files and the integration defects beyond conflict markers:
triaging is a declared active graph node; application workspace checks wait until triage
finishes; assessments, timestamps and custom audit events share one SQLite transaction;
only persisted triage holds with explicit audited release can return to intake. Both TUI
configuration tabs remain available. Added application triage restart/release, invalid
release and audit-write rollback regression coverage.

Reproduction: after textual conflict resolution, test collection failed because TRIAGING
was absent from the lifecycle graph. The focused integrated regression suite now passes
56 tests. Extracted-wheel SQLite triage/hold/release/restart smoke, Ruff, compileall, lock
validation, wheel/sdist builds, 12/12 offline contracts and scripted repair evaluation pass.
No static type checker or hosted preview is configured; no live provider/deployment run.
Full-suite result and publication/review follow-up are recorded below.

Final full suite: 420 passed in 84.36s, 94.56% total coverage; all per-file gates passed.
The first full run found one incorrect new-test expectation about the existing intake event;
corrected it to assert the audit history is unchanged, then reran the complete suite green.

Committed/pushed integration fix: `f5043fdf3a7a4fb3cf6c678cd6c1e6e37975dfc6`.
Updated PR #23 title/body with the final scope, integration cause and validation evidence.
Initial post-push check at 2026-09-18 11:55:50 UTC: open, clean and mergeable; no general
or inline comments, reviews or CI checks. Follow-up at 12:01:10 UTC (5m20s later): still
open, clean and mergeable at the same commit, with no comments, reviews or CI checks.
No feedback required changes. This final handoff-only commit records the follow-up;
monitoring ends here, and no background monitor is installed.


## Architecture-aware repair and release — 2026-09-19

Implemented with requested Luna agents and root orchestration in isolated worktree
`/Users/iamin/work/code/tools/incident-harness-release`, branch
`feat/architecture-conflict-release`, based on `origin/master`. Original checkout edits
(README.md, TODO.md, VENDORS.md and dashboard proposal) were preserved. No VISION.md exists.

Adds scoped vision/architecture discovery with explicit missing/omitted documentation,
normal-versus-architectural repair guidance, and pinned MIT-attributed Matt Pocock
code-review, codebase-design, and resolving-merge-conflicts skill adaptations. Code review
uses separate Standards and Spec axes. Conflicting tracked PRs are detected by signed
webhooks and five-minute GitHub polling, merged against their actual base with durable
lease-protected recovery, verified afresh, and pushed normally. Application member scope,
retry budgets, stale/unknown head handling, crash cuts, and publication state are covered.
Prepares v0.2.0 wheel/curl installer and source-preserving update behavior.

Validation: 579 tests passed in 90.92s; 94.01% aggregate coverage and every module >90%.
Ruff, compileall, uv lock --check, diff checks, wheel/sdist builds, all 12 offline contracts,
and scripted repair evaluation passed. Clean Linux wheel installation and isolated macOS
uv installation passed CLI help, offline eval, version and all three skill/license checks.
Final wheel modules and skills match source bytes. Independent final audit found no blockers.
No live model or hosted preview deployment exercised; architecture judgment is agent guidance.

Commit `51e30d1f607ad4eb42e397d572249a54a757ac8a`; PR #27:
https://github.com/Isaac12x/incident-resolver-agent/pull/27
Initial review check at 2026-09-19 13:00 UTC: open, mergeable, no general/inline comments,
reviews, or configured CI checks. Five-minute follow-up and public release pending.


## Stable/nightly updates and v0.3.0 — 2026-09-19

Root orchestration with the requested Luna implementation agent on
`feat/release-nightly-update`, based on `origin/master` at `6b402cb`. No VISION.md exists.
Prepares v0.3.0 with the merged plugin, execution-browser, CLI-help and TUI changes.
Adds `incident-agent update [stable|nightly]`: nightly refreshes Git default-branch
source; stable resolves the latest wheel, including a return from GitHub nightly
installs and fork provenance. Explicit custom sources remain supported, and nightly
rejects release-only overrides. Removed existing changelog conflict markers.

Validation so far: 52 targeted tests, repository Ruff, compileall, lock check, wheel
and sdist build, 12 offline contracts and scripted repair evaluation passed. Isolated
uv wheel install exposes nightly help. Real uv same-version Git update smoke advanced
across multiple commits. Full-suite verification, PR review monitoring and publication
results are recorded in the follow-up below. No static type checker or hosted preview
is configured. Restart running services after updating.

Pre-publication verification: full suite passed (642 tests, 94.25% coverage; all
per-file gates passed) with a test-process-only `ulimit -n 4096`. macOS's default
256 descriptors caused the earlier deletion/rename failure after accumulated SQLite
connections; the unchanged isolated test and module passed. Separate coverage output
avoided collisions with parallel reviewer runs. After eight additional updater
failure/metadata regression cases, all 60 focused tests passed; production code was
unchanged. Ruff and diff checks passed. Both Luna review axes found no blocking code
issues; two old release examples were corrected. The installed wheel successfully
self-updated to the remote PR branch at the same version.

PR #30: https://github.com/Isaac12x/incident-resolver-agent/pull/30
Initial check at 16:04:16 UTC: open, clean and mergeable, with no comments, reviews
or configured CI checks. Five-minute follow-up, merge and publication follow.

Five-minute follow-up at 16:09:26 UTC: PR #30 remained clean and mergeable at
`9cb4eef`, with no general/inline comments, reviews or configured CI checks. No
feedback needed integration. Monitoring ends after this release; no background
monitor is installed. Publish v0.3.0 from the merged PR commit with the wheel,
source archive, installer and SHA256SUMS, then verify public stable/nightly updates
in the isolated uv tool environment. Release URL:
https://github.com/Isaac12x/incident-resolver-agent/releases/tag/v0.3.0

## 2026-09-20 — Runtime versus Sources investigation

User requested Luna subagents to explain why Plugins/Connect and Sources exist after runtime setup. Two subagents traced the runtime intake path and TUI/CLI configuration path. No application code changed; existing src/tui.py and src/tui_theme.py edits were left untouched.

Findings: Runtime config defines the listener/execution settings. Named webhook intake requires a configured connector for /hooks/incidents/{connector}; direct normalized submit_incident intake does not. Sources edits Config.connectors; CLI connect writes that same list, while plugins only lists static adapter metadata. Existing configured sources do not require a second connection step. Evidence sources (Loki/Grafana/local logs/MCP) remain distinct from receiving alert metadata. Active bundles may override live config and require rebuild/activation plus restart.

Suggested UX: one Sources management view populated from existing configuration, separating incident intake from evidence/tools; plugins/connect remain CLI alternatives, not additional setup requirements. Runtime trigger preferences include currently inert fields. Webhook Test connection currently reports no runtime adapter configured despite webhook intake being server-managed. This was an explanation/research request, so no implementation, tests, commit, or PR was performed.

## 2026-09-19 — Sources investigation guidance

Added the exact requested sentence as the first element of the TUI Sources page:
"Connect to your logs to get the agent access to investigation evidence needed to fix the bug".
Updated the Unreleased changelog. Verified the exact first-element text, the focused
Sources test, all 15 selected TUI/theme tests, Ruff, diff checks, and wheel/sdist build.
No type checker is configured. No deployment verification applies to this local TUI copy change.
Changes isolated on fix/sources-evidence-guidance; other workspace edits preserved.

PR: https://github.com/Isaac12x/incident-resolver-agent/pull/33
Five-minute review follow-up: no general comments, inline comments, reviews, or
configured CI checks; merge state CLEAN. No feedback required changes. Monitoring
completed for this turn; no background monitor remains active.

## 2026-11-12 — CLI color and tables

Implemented with Luna implementation, test/spec, and standards-review agents under
root orchestration on `feat/cli-color-tables`. No VISION.md exists.
Doctor/status now render STATUS/CHECK/DETAILS tables with green OK and red FAIL
labels; plugins and configured-source listings also use aligned tables. A shared
dependency-free renderer handles ANSI-aware padding, multiline cells and wrapped
final columns. NO_COLOR and TERM=dumb disable color; FORCE_COLOR enables redirected
color, otherwise detection follows the output TTY. Doctor/status and connect --list
gain JSON modes; installer chatter goes to stderr with doctor --install --json.
Exit codes and existing plugins JSON behavior are preserved. Consumers of the old
tab-separated connect --list output should use its new --json mode. Very narrow
terminals or unusually wide leading columns can still exceed terminal width.

Validation: 36 focused tests and the full 670-test suite pass (94.46% total coverage,
98% formatter coverage), using a test-process descriptor limit of 4096. Repository
Ruff, compileall, lock check, wheel/sdist build and diff checks pass. No static type
checker or hosted preview is configured. Independent standards/spec reviews found
no blocking issues. Publication and five-minute review follow-up recorded below.

PR #48: https://github.com/Isaac12x/incident-resolver-agent/pull/48
Published commit dd547bc. Initial check at 16:18:37 UTC and five-minute follow-up
at 16:23:38 UTC: open, CLEAN/mergeable, no general or inline comments, reviews,
or configured CI checks. No feedback required integration. The GitHub connector
returned an integration-permission 403 for PR creation; authenticated gh succeeded.
Review monitoring completed for this turn; no background monitor remains active.

## 2026-11-13 — Installed CLI version reporting

Implemented with Luna source, test/spec and standards-review agents, root orchestrating,
on feat/cli-version from origin/master 3f761a2 (prior CLI formatting PR #48 merged).
No VISION.md exists. Original `uv run incident-agent version` exited 2 as unknown.
Updates already use uv plus installed provenance and release resolution; they do not
require a CLI version command. Added version, global --version and version --json
using installed distribution metadata. Optional recorded VCS commit distinguishes
same-version nightly revisions; raw source URLs are never printed. Missing package
metadata reports unknown, and absent/malformed optional provenance yields null commit.
Version dispatch precedes config resolution and does not bootstrap, probe tools or
contact the network. Updater behavior is unchanged. README/help/changelog updated.

Validation: full suite 678 passed, 94.46% coverage (version module 93%). Two further
metadata edge-case tests and stronger side-effect assertions were added afterward;
final focused suite 10 passed, targeted version coverage 100%. A targeted coverage
invocation hit unrelated global per-file gates, so it is not a full-suite success.
Ruff, compileall, lock check, build and diff checks pass. Built wheel smoke installed
in an isolated uv environment outside checkout correctly reports version JSON.
No static type checker or hosted preview is configured. Standards: no issues; spec:
no blocking issues. Reviewer suggested restricting VCS commit to Git; retained generic
commit reporting because CLI labels it generically and other VCS revisions are valid.
Publication and review follow-up recorded below.
