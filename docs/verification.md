# TODO implementation verification — 2026-09-14

Acceptance mapping: [TODO.md](../TODO.md). Implemented in the isolated `feat/todo-platform`
worktree to preserve the original checkout's unresolved installer conflict and local changes.

## Executed checks

- Full `pytest -q`: **233 passed**, **95.14%** total coverage; every source module passed the
  repository's strict **greater than 90%** gate. The run emitted 73 resource warnings, mainly
  SQLite connections from existing test/backend paths; it did not fail any assertions or gates.
- Ruff and `git diff --check`: passed. `uv lock --check`: passed. Installer shell syntax: passed.
- `uv build`: wheel and source distribution built successfully. No static type checker is configured.
- Installed the built wheel through `install.sh` into isolated temporary uv tool/bin directories.
  From an unrelated working directory, the installed CLI ran all 12 contract cases and the repair
  suite; active bundle loading found the packaged built-in skills and versioned prompt.
- Actual Docker-compatible runtime with `busybox:1.37.0-musl`: workspace writes succeeded;
  read-only writes, host-sensitive-file access, host-root writes, and network access failed;
  a timed-out command was removed. No managed test containers remained.
- Real pinned wheel installation/invocation/restart restoration and concurrent policy updates were
  exercised. Summary reads were verified against generated investigation/fix artifacts and the
  explicitly labelled extractive fallback used before those artifacts exist.
- Task catalog tests cover concurrent deduplication, durable worker exclusion, cancellation release,
  legacy migration, workspace replacement, and recovery after deleting artifact folders.
- Version tests cover tampering, installed application loading, and rollback to the immediately
  previous activation when three versions exist; unexpected nested manifests are rejected.
- Installed helper resolution preserves the virtual environment across Python symlinks, and
  managed tool install/update maps the `seed` executable to the `seed-cli` package.

## Evaluation results and limits

- Contract suite: 12/12 passed.
- Scripted repair through the actual WorkflowEngine: completed, success 1, unsafe attempts 0.
  An independent regression failed before repair and passed after it. False-success/no-op and
  test-tampering cases were rejected. Monetary cost is `null`, not estimated.
- Synthetic chronological root-cause fixture: 2 training rows, 2 holdout rows, 2 correct predictions.
- Synthetic retrieval fixture: 4/4 at recall@5 using the explicitly reported lexical fallback.
  This tiny fixture includes self-retrieval and verifies report/dispatch behavior, not ranking quality.
- A prior optional-dependency smoke in this PR used real sentence-transformer embeddings (384
  dimensions) and FAISS and returned the expected related incident. The base installed environment
  does not include those optional dependencies.

These results establish local harness behavior, not production model accuracy or calibrated confidence.
Summary explanations use generated investigation/fix artifacts when available and an explicitly
labelled extractive fallback before they exist. Native subscription CLI tools, trusted extensions,
and MCP servers are not confined by the optional repository-command container boundary. No preview
deployment is configured.


## Clarified summarization integration

The user identified “explain-code” as HumanLayer's `show-me` skill. The local adaptation now applies
in investigation, implementation, and the durable lead session. The summary API returns persisted
Markdown from fix/investigation artifacts, or an explicitly labelled intake fallback before those
artifacts exist. The unused external command adapter/configuration and its five provider-specific
tests were removed; a lifecycle/restart/API regression was added. Generic bounded subprocess tests
remain for trusted extensions. The new wheel includes the expanded skill. Full checks above were
rerun after this correction; model-generated prose quality was not evaluated with a live model.


## File-backed lifecycle graph — 2026-09-18

Implemented declarative lifecycle transitions, atomically committed with their events, while
retaining the durable agent loop. Harness state, SDK sessions, intake/history, telemetry and
operation journals now use JSON files with process locks and atomic replacement. Existing
SQLite data is migrated read-only; the third-party repository graph remains a derived index.

The file-only runtime regression initially failed because Storage created sessions.sqlite3;
it now passes and verifies state/messages/events from a separate process. Additional checks
exercise real legacy SQLite imports, concurrent processes, interrupted writes, corruption,
empty-session migration, operation restart budgets, and stale deployment-cache rejection.
Test fixtures that previously skipped lifecycle stages now restore explicit checkpoints;
production transition validation is not disabled for tests.

Validation: 311 tests passed; 95.39% overall coverage and every source module >90%. Ruff,
compileall, uv lock --check, wheel/sdist builds, all 12 offline contracts, and the scripted repair
lifecycle evaluation passed (completed; no unsafe attempts). An extracted-wheel smoke test
confirmed task and SDK session persistence without creating SQLite files.
No static type checker is configured. No live model, production deployment, or remote
preview verification was performed. Performance/token improvements are not benchmarked.
Stop old workers before migration; preserve the full runtime directory and legacy databases.


## SQLite runtime state — 2026-09-18

Replaced whole-document runtime JSON updates with indexed tables in one `runtime.sqlite3`.
Tasks and transition events, leases, workspace identities, conversations, SDK session items,
observability/history, metric counters, and operation attempts now use SQLite transactions.
Readable artifacts and rotating JSONL logs remain files. Migration markers prevent stale JSON
or older SQLite sources from overwriting committed data; original sources are preserved.

Validation: 345 tests passed, 95.18% aggregate coverage, every source module above 90%.
Tests cover transaction rollback, process death before commit, WAL readers during writes,
concurrent deduplication, leases, session appends, metric increments, operation budgets,
source precedence, corrupt imports, and restart behavior. The test run emitted SQLite
ResourceWarnings; assertions and coverage gates passed. Ruff, compileall, uv lock --check,
and wheel/sdist builds passed. No static type checker is configured.

All 12 offline contracts and the scripted repair lifecycle passed (completed, zero unsafe
attempts). An isolated extracted-wheel smoke verified fresh runtime creation and migrated
a fixture produced by the previous committed JSON implementation. It retained task state,
ordered events, conversations, alert/history data, SDK history, metrics and operation budgets;
all seven original JSON sources remained byte-identical. Session clearing and lease exclusion
survived restarts; SQLite integrity and foreign-key checks passed. No live model, production
deployment, or remote preview was exercised. Performance improvements were not benchmarked.

Stop old workers before upgrading, keep the complete runtime backup, and use SQLite's backup
API for live database backups rather than copying only the main file while WAL is active.
