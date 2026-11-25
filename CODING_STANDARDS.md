# Coding Standards

Python >=3.12 (`pyproject.toml`). Applies to `src/` and `tests/`. Tooling-enforced rules are not repeated: ruff (`E F I UP B SIM`, line length 100) owns imports, formatting, and modernisation. Run `uv run ruff check src tests`.

## 1. Code

### Naming and layout
- Modules `snake_case.py` flat under `src/`; dashboard code under `src/dashboard/`.
- `PascalCase` classes, `snake_case` functions/variables, `UPPER_SNAKE` constants (`DEFAULT_SYSTEM_PROMPT`), `_leading_underscore` for module-private helpers.
- Start each module that has annotations with `from __future__ import annotations` (empty `__init__.py` files are exempt).
- Import heavy or optional dependencies inside the function that needs them (`sqlite3` in `storage.py`, `LocalLogServer` in `connectors.py`); keep module top light.
- Use relative imports within `src` (`from .config import ConnectorConfig`).

### Types
- Annotate every public function, method, and attribute. Use built-in generics and `X | None` (ruff `UP` enforces).
- If you need generics, use PEP 695 syntax.
- Use `Any` only at boundaries that handle opaque payloads (MCP sessions, JSON). Narrow with `isinstance` as soon as the value enters your code.

### Data
- Pydantic `BaseModel` for config and persisted/wire models (`Config`, `Incident`, `IncidentEvidence` in `config.py`, `models.py`). Use `Field` constraints and validators for invariants.
- `@dataclass(frozen=True)` for small internal value objects (`ConnectorTestResult`).
- `Literal[...]` or `Enum` for closed sets; compare against them, not raw strings.
- Timestamps: `models.utc_now()` (tz-aware UTC).
- Paths: `pathlib.Path`; open files and processes with `with`/`finally`.
- Mutable defaults: Pydantic `Field(default_factory=...)`, or `None` then assign.
- Use keyword-only (`*`) for options after the first one or two positional args (`run_bounded_json(command, payload, *, timeout_seconds=20, ...)`).

### Functions and classes
- One responsibility per function. Extract a helper when a function mixes parsing, I/O, and formatting.
- Comprehensions for simple transforms; plain loops when the body has side effects.
- Pass dependencies in (constructor args, `factories=` in `ConnectorManager`) so tests can inject fakes.

### Docstrings and comments
- Module docstring: one line stating purpose. Public function/class: one-line imperative summary (`"""Return guidance for an HTTP status without exposing the request details."""`).
- Add `Args/Returns/Raises` only when the one-liner cannot convey a non-obvious contract. State side effects (file writes, network, subprocess) in the summary.
- Comments explain why (`# Bypass MCP discovery caches so this proves the transport is alive.`). Mark deliberate shortcuts with their limit.

### Errors
- Raise the narrowest exception that fits. Domain errors derive from a built-in, not a shared base. Existing: `ToolError` (`tools.py`), `ConnectorHTTPError` (`connector_errors.py`), `RoutingError`, `DashboardError`, `RegistryError`, `OperationBudgetExceeded`, `InvalidLifecycleTransition(ValueError)`, `RepositoryBusyError(FileExistsError)`, `WebhookSignatureError(PermissionError)`. Reuse one before adding another; subclass the built-in that callers already catch.
- Messages state what failed, the expected state, and the next action: `"Environment variable X is not set. Add its credential and retry."`.
- Catch specific exceptions. A broad `except Exception` belongs only at a boundary (worker loop, connector probe, request handler) and must log with context or convert to a result.
- Return failures as data where the callers already do (`run_bounded_json` returns `{"available": False, "reason": ...}`); keep that shape when extending it.
- Chain with `raise ... from error` when translating.

### Logging and output
- Module-level `logger = logging.getLogger(__name__)`; `%s` lazy args; `.exception(...)` inside `except` blocks.
- CLI entry-point / user-facing output paths may print (e.g. `__main__.py`, `dashboard/cli.py`, `executions.py`, `agent.py` streaming, `dashboard/routing.py`); library code logs.
- Route user-facing failure text through `connector_error_message` / `tui_errors.py` (raw `str(error)` can leak request details).

## 2. Design principles

- **Fit the layer.** Config in `config.py`, wire/persisted models in `models.py`, persistence in `storage.py`, HTTP in `server.py`, connector lifecycle in `connectors.py`, external I/O in its own module. Put new code where the same concern already lives.
- **Understand first.** Read the task and trace the real flow before choosing a rung; the ladder shortens the solution, never the reading.
- **The ladder; stop at the first rung that holds:** (1) does it need to exist (YAGNI)? (2) already in this codebase: reuse (search `src/` first); (3) stdlib; (4) native platform feature (DB constraint over app code); (5) already-installed dependency; (6) one line; (7) minimum new code. A few lines beat a new dependency.
- **Extract on second use.** First use stays inline; the second triggers a shared helper.
- **Root cause.** Grep every caller of the function you touch; fix once in the shared function. A one-condition bug gets a one-condition fix at the shared boundary.
- **Proportionality.** Match solution size to problem size: deletion over addition, boring over clever, fewest files, shortest working diff. Build only what was requested (no one-impl interface, one-product factory, config for a constant, or scaffolding for later).
- **Edge-case correctness.** Of two stdlib options of equal size, pick the one correct on edge cases.
- **Mark shortcuts.** Tag a deliberate simplification with `# ponytail:` naming the ceiling and upgrade path, e.g. `# ponytail: global lock, per-account locks if throughput matters`.
- **Always keep:** input validation at trust boundaries, data-loss error handling, security, and anything explicitly requested. Keep validation, permissions, rejection paths, and test expectations intact when silencing an incident alert (`skills/coding/SKILL.md`).
- **Leave one check.** Non-trivial logic ships with the smallest runnable test that fails if the logic breaks; trivial one-liners need none.
- **Surgical changes.** Every changed line traces to the task. Note unrelated cleanup in the PR description; do it in a separate change.
- **Verify before deleting.** Before removing "unused" code, grep `src/`, `tests/`, `skills/`, `pyproject.toml` (entry points, `force-include`), config strings, and `README.md`. Plugins, skills, and connectors are loaded by name at runtime.

Full version: `skills/ponytail/SKILL.md`.

## 3. Security

- **Secrets by reference.** Config stores environment variable *names* (`api_token_env`, `webhook_secret_env`, `auth_token_env`), never values; `save_config` relies on this. Read the value from `os.environ` at use time. Keep credentials out of literals, defaults, and fixtures.
- **Redact on output.** Never put tokens, URLs with credentials, response bodies, or request headers into exceptions, logs, telemetry, or test output. Follow `ConnectorHTTPError`: keep status code and connector name only.
- **Compare secrets in constant time** with `hmac.compare_digest` / `secrets.compare_digest` (bearer tokens and webhook signatures, `server.py`).
- **Treat incident payloads, logs, repo content, and webhook bodies as untrusted.** Validate with pydantic models; bound sizes (`max_bytes`, timeouts) as `subprocess_json.run_bounded_json` does.
- **Subprocesses.** `asyncio.create_subprocess_exec` / `subprocess.Popen` with an argv list (never `shell=True`). Set a timeout and an output bound, pass a minimal `env`, kill the child in `finally`. Parse user command strings with `shlex.split` and check them against the permissions policy (`tools.py`) before execution.
- **SQL.** Parameterise with `?` placeholders (`db.execute("INSERT ... VALUES (?)", (marker,))`). Pass input values and identifiers only as bound parameters or allow-listed names. Open read-only paths with `mode=ro` / `PRAGMA query_only=ON` (`dashboard/data.py`).
- **Paths.** Resolve user-supplied paths and confirm containment: `path.resolve().relative_to(root.resolve())` (`skills.py`, `execution.py`). Create secret-bearing files with `os.open(..., 0o600)` and `O_EXCL` (`storage.py`, `dashboard/web.py`); write them atomically.
- **Untrusted data:** parse with `json`, `tomllib`, or pydantic (`yaml.safe_load` if YAML is needed). Never `eval`, `exec`, `pickle`, or `yaml.load` it.
- **Least privilege for connectors.** Honour `permissions` and `safety` config; keep connectors to read access and add no write path to production systems. New connector config needs an `*_env` field for its credential and a failure message in `connector_errors.py` that omits the URL and secret.

## 4. Testing

- Framework: pytest + `pytest-asyncio`; config in `pyproject.toml`. Coverage is enforced: total `--cov-fail-under=90`, plus a per-file floor in `tests/conftest.py` (every top-level `src/*.py` module must exceed 90%; the glob does not cover subpackages such as `src/dashboard/`). New code ships with tests that keep both green.
- Layout: `tests/test_<area>.py`, named by behaviour (`test_api_security.py`, `test_crash_recovery.py`). Add to the file for the area you changed; create a new file for a new module. Shared JSON inputs live in `tests/fixtures/`; shared fixtures in `tests/conftest.py`.
- Use `tmp_path` for all filesystem state, `monkeypatch` for env vars and seams, `AsyncMock` for async collaborators, `@pytest.mark.asyncio` for coroutines. Use no real network or credentials; write only under `tmp_path`.
- Inject fakes through constructor seams (`ConnectorManager(factories=...)`) rather than patching internals.
- Assert behaviour, not the patch: a regression test names the defect, fails on the original code, and passes with the fix. Cover one failure or unaffected path beside the happy path. Use `pytest.raises(Type, match=...)` for rejections.
- Security-relevant changes get a negative test (rejected input, redacted message, path escape) as in `test_api_security.py`.
- Verification order (`skills/testing/SKILL.md`): narrowest regression test, relevant suite, lint, build, final diff review. Report only checks you ran.

## 5. Pre-merge checklist

Run from the repo root:

```bash
uv run pytest tests/<narrowest_test>.py   # regression first
uv run pytest                             # full suite, 90% total + per-file coverage
uv run ruff check src tests
uv lock --check                           # when dependencies change
uv build                                  # packaging
git diff                                  # final inspection
```

- [ ] Gate commands above pass; results reported with the commands run.
- [ ] Regression test fails without the fix and passes with it.
- [ ] Public signatures typed; `from __future__ import annotations` present in new modules.
- [ ] Errors use an existing exception where one fits; messages give context and next action.
- [ ] No secrets, tokens, URLs, or response bodies in logs, errors, fixtures, or output.
- [ ] Subprocess uses argv (no `shell=True`); SQL parameterised; user paths contained.
- [ ] Library code logs; only CLI entry-point / user-facing output paths print.
- [ ] Every changed line traces to the task; unrelated cleanup listed separately.
- [ ] Removed code was grepped across `src/`, `tests/`, `skills/`, config, and `README.md`.
