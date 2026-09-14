# TODO implementation verification — 2026-09-14

Acceptance mapping: [TODO.md](../TODO.md). Implemented in the isolated `feat/todo-platform`
worktree to preserve the original checkout's unresolved installer conflict and local changes.

## Executed checks

- Full `pytest -q`: **237 passed**, **95.20%** total coverage; every source module passed the
  repository's strict **greater than 90%** gate. The run emitted 64 resource warnings, mainly
  SQLite connections from existing test/backend paths; it did not fail any assertions or gates.
- Ruff and `git diff --check`: passed. `uv lock --check`: passed. Installer shell syntax: passed.
- `uv build`: wheel and source distribution built successfully. No static type checker is configured.
- Installed the built wheel through `install.sh` into isolated temporary uv tool/bin directories.
  From an unrelated working directory, the installed CLI ran all 12 contract cases and the repair
  suite; active bundle loading found the packaged built-in skills and versioned prompt.
- Actual Docker-compatible runtime with `busybox:1.37.0-musl`: workspace writes succeeded;
  read-only writes, host-sensitive-file access, host-root writes, and network access failed;
  a timed-out command was removed. No managed test containers remained.
- Real pinned wheel installation/invocation/restart restoration, real explain subprocess success,
  malformed/oversized output and timeout, and concurrent policy updates were exercised.
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
The exact intended explain-code upstream remains unidentified; the configurable JSON provider contract
is implemented and tested. Native subscription CLI tools, trusted extensions, and MCP servers are not
confined by the optional repository-command container boundary. No preview deployment is configured.
