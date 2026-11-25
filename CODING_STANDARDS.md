# Coding Standards

Two parts. Part 1 is normative for all new and changed code. Part 2 adds what is specific to incident-harness.

# Part 1: Generic standards

## Naming and imports
- `PascalCase` classes, `snake_case` functions/variables, `UPPER_SNAKE` constants, `_leading_underscore` for private names.
- Import order: stdlib, third-party, local; one blank line between groups.

## Idioms
- Comprehensions for simple transforms; loops when the body has side effects.
- `with` for files, locks, DB connections, and processes.
- `dataclass` / `NamedTuple` / `Enum` for data and closed sets (pydantic `BaseModel` for validated/wire data). Compare against enum members, not raw strings.
- `pathlib.Path` for paths. tz-aware `datetime` only.
- Keyword-only (`*`) when a function has more than one optional argument.
- Mutable defaults: use `None` then assign, or `field(default_factory=...)`.
- State flows through parameters and return values; no `global` / `nonlocal`.

## Functions and classes
- Small, single responsibility. Split a function that mixes parsing, I/O, and formatting.
- Flag long or branchy functions that resist unit testing; extract until each piece is unit-testable.
- Docstrings on every public module, class, function, and method with `Args`, `Returns`, `Raises`. Document side effects (file writes, network, subprocess, mutation).
- Member order in a class: class variables, `__init__`, public methods, private methods, properties.
- Pass dependencies in (constructor args) so tests can inject fakes.

## Types
- Hints on all public APIs, module variables, and class attributes.
- Generics use PEP 695 syntax (3.12+): `def first[T](items: list[T]) -> T:`.
- `Any` only in thin wrappers over opaque payloads; narrow with `isinstance` at the boundary.

## Errors
- Raise the narrowest specific exception. Catch specific exceptions; never a bare `except`.
- `except Exception` only at an application boundary (worker loop, request handler), and always log with context.
- Inside `except`, log with `logger.exception(...)`.
- Messages give context, the expected state, and guidance. Chain with `raise ... from error`.

```python
# Before
raise ValueError("bad config")
# After
raise ConfigError(
    f"Config file {path} is missing 'timeout'. Expected an integer number of seconds; add one and retry."
)
```

- Domain exceptions derive from one application base exception:

```python
class AppError(Exception): ...
class ConfigError(AppError): ...
class InputError(AppError): ...
```

## Security
Each ban pairs with its replacement.
- No `eval` / `exec` / `pickle` / `yaml.load` on untrusted data. Parse with `json`, `tomllib`, pydantic, or `yaml.safe_load`.
- No hard-coded secrets. Read credentials from the environment or a secret store at use time.
- Subprocess: argv list, never `shell=True`; set a timeout and output bound.
- SQL: parameterised placeholders; identifiers only from an allow-list.
- Paths from users: resolve, then confirm containment (`path.resolve().relative_to(root.resolve())`).
- Compare secrets with `hmac.compare_digest`.
- Tokens, credentialed URLs, and response bodies stay out of logs, errors, and test output.
- Least privilege: request only the access the task needs; no write path where read suffices.

## Design principles
- **Maintainability:** comments explain why, not what. Use `logging` with lazy `%s` args; `print` only for CLI user output.
- **Architectural fit:** follow existing patterns and layers; put code in the module where the same concern already lives.
- **DRY and simplest implementation:** first use stays inline; the second concrete use triggers a shared helper or abstraction, not before.
- **Surgical changes:** every changed line traces to the task's purpose. Flag unrelated cleanups separately; do them in another change.
- **Verify before deleting:** "unused" code may be a protocol method, hook, public API, entry point, or name-loaded plugin. Grep code, config, entry points, and docs first.

## Ponytail
- **Ladder; stop at the first rung that holds:** (1) does it need to exist (YAGNI)? (2) already in the codebase: reuse; (3) stdlib; (4) native platform feature (DB constraint over app code); (5) already-installed dependency; (6) one line; (7) minimum new code. A few lines beat a new dependency.
- **Understand first:** read the task and trace the real flow before picking a rung.
- **Root cause:** grep every caller of the function you touch; fix once in the shared function.
- **Proportional build:** deletion over addition, boring over clever, fewest files, shortest working diff. No one-impl interface, one-product factory, config for a constant, or scaffolding for later.
- **Edge cases:** of two equal-size stdlib options, pick the one correct on edge cases.
- **Mark shortcuts:** `# ponytail:` naming the ceiling and upgrade path, e.g. `# ponytail: global lock, per-account locks if throughput matters`.
- **Never simplify away:** input validation at trust boundaries, data-loss error handling, security, anything explicitly requested.
- **Leave one runnable check:** non-trivial logic ships with the smallest test that fails if the logic breaks; trivial one-liners need none.

# Part 2: incident-harness specifics

Python >=3.12, ruff (`E F I UP B SIM`, line length 100), pytest + `pytest-asyncio`.

## Conventions
- Start each module that has annotations with `from __future__ import annotations` (empty `__init__.py` exempt).
- Timestamps: `models.utc_now()`.
- Config stores environment variable *names* in `*_env` fields (`api_token_env`, `webhook_secret_env`); never values. A new connector credential needs an `*_env` field and a message in `connector_errors.py` that omits URL and secret. Route user-facing failure text through `connector_error_message` / `tui_errors.py`.
- Inline `logging.getLogger(__name__)` sites: `app.py`, `workflow.py`.
- Modules where `print` is allowed: `__main__.py`, `dashboard/cli.py`, `dashboard/routing.py`, `executions.py`, `agent.py`.
- External commands: `subprocess_json.run_bounded_json(command, payload, *, timeout_seconds=20, max_bytes=...)`; failures return data (`{"available": False, "reason": ...}`); keep that shape.
- Secret-bearing files: `os.open(..., 0o600)` with `O_EXCL` where creating new (`storage.py`, `dashboard/web.py`); `dashboard/process.py` and `dashboard/routing.py` also use `0o600`; write atomically.
- `compare_digest` sites: bearer token and webhook signature (`server.py`), `github.py`, `agent.py`, `dashboard/web.py`.
- Layers: config `config.py`; wire/persisted models `models.py`; persistence `storage.py`; HTTP `server.py`; connector lifecycle `connectors.py`.
- No `AppError` exists yet. Interim rule: new domain exceptions subclass the built-in that callers catch, and move under an `AppError` base once one is added. Existing exceptions (reuse before adding): `ToolError` (`tools.py`), `ConnectorHTTPError` (`connector_errors.py`), `RoutingError` (`dashboard/routing.py`), `DashboardError` (`dashboard/process.py`), `RegistryError` (`extensions.py`), `OperationBudgetExceeded` (`operations.py`), `InvalidLifecycleTransition(ValueError)`, `RepositoryBusyError(FileExistsError)`, `WebhookSignatureError(PermissionError)`.
- Before deleting "unused" code, also grep `skills/`, `pyproject.toml` (`force-include`), and `README.md`; plugins, skills, and connectors load by name.
- Skills: `skills/coding/SKILL.md`, `skills/testing/SKILL.md`, `skills/ponytail/SKILL.md` (full Ponytail ladder).

## Testing and coverage
- Coverage: total `--cov-fail-under=90` (`pyproject.toml`) plus a per-file floor of 90% in `tests/conftest.py`, applied to top-level `src/*.py` only (not `src/dashboard/`). New code keeps both green.
- Layout: `tests/test_<area>.py`; fixtures in `tests/fixtures/` and `tests/conftest.py`. Use `tmp_path`, `monkeypatch`, `AsyncMock`, `@pytest.mark.asyncio`; no real network or credentials.
- Inject fakes through seams (`ConnectorManager(factories=...)`).
- A regression test names the defect, fails on the original code, passes with the fix; security changes add a negative test (`test_api_security.py`).

## Known gaps
Current code falls short of Part 1 in these places:
- 300+ `Any` uses across `src/`.
- No application base exception; domain errors derive from built-ins.
- `server.py` uses `nonlocal` (line 48).
- Most docstrings are one-liners without `Args` / `Returns` / `Raises`.
- No PEP 695 usage.
- No type checker configured (`mypy` / `pyright` absent from `pyproject.toml`).

New and changed code meets Part 1. Fix gaps opportunistically, within the scope of the change you are making.

# Pre-merge checklist

```bash
uv run pytest tests/<narrowest_test>.py   # regression first
uv run ruff check src tests
uv run pytest                             # full suite + coverage floors
uv build
uv lock --check                           # when dependencies change
git diff                                  # final inspection
```

- [ ] Gate commands pass; report only commands you ran.
- [ ] Regression test fails without the fix, passes with it; security changes have a negative test.
- [ ] Names and imports follow convention; tz-aware `datetime`, `pathlib`, no `global` / `nonlocal`.
- [ ] Public APIs typed and documented (`Args` / `Returns` / `Raises`, side effects); `Any` only in thin wrappers.
- [ ] Specific exceptions with context + guidance, existing ones reused; no bare `except`; broad catch only at a boundary, logged.
- [ ] No secrets, tokens, URLs, or bodies in code, logs, errors, fixtures; `compare_digest` for secrets; least privilege.
- [ ] No `shell=True`, `eval`/`exec`/`pickle`; SQL parameterised; user paths contained.
- [ ] Logging in library code; `print` only in CLI modules.
- [ ] Code sits in the right layer; existing helpers reused; Ponytail ladder followed, no speculative abstraction.
- [ ] Every changed line traces to the task; unrelated cleanup listed separately.
- [ ] Removed code was grepped (code, tests, skills, config, entry points, docs).
- [ ] `from __future__ import annotations` in new modules; `models.utc_now()` for timestamps.
