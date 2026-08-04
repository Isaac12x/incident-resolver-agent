"""Repository-wide pytest policy checks."""

from __future__ import annotations

from pathlib import Path

import pytest

MINIMUM_FILE_COVERAGE = 90.0
PER_FILE_COVERAGE_FAILED = pytest.StashKey[bool]()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtestloop(session: pytest.Session):  # noqa: ANN201
    """Fail the suite when any source module violates the vision's coverage floor."""
    result = yield
    coverage_plugin = session.config.pluginmanager.getplugin("_cov")
    if coverage_plugin is None or coverage_plugin.cov_controller is None:
        return result

    coverage = coverage_plugin.cov_controller.cov
    measured = {Path(path).resolve() for path in coverage.get_data().measured_files()}
    failures: list[tuple[Path, float]] = []
    for path in sorted((Path.cwd() / "src").glob("*.py")):
        resolved = path.resolve()
        if resolved not in measured:
            failures.append((path, 0.0))
            continue
        _, statements, _, missing, _ = coverage.analysis2(str(resolved))
        covered = len(statements) - len(missing)
        percentage = covered * 100 / len(statements) if statements else 100.0
        if percentage <= MINIMUM_FILE_COVERAGE:
            failures.append((path, percentage))

    if failures:
        reporter = session.config.pluginmanager.getplugin("terminalreporter")
        reporter.write_sep("=", "per-file coverage failures", red=True, bold=True)
        for path, percentage in failures:
            reporter.write_line(
                f"{path.relative_to(Path.cwd())}: {percentage:.1f}% "
                f"(must be > {MINIMUM_FILE_COVERAGE:.0f}%)",
                red=True,
            )
        session.testsfailed += 1
        session.config.stash[PER_FILE_COVERAGE_FAILED] = True
    return result


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session) -> None:
    if session.config.stash.get(PER_FILE_COVERAGE_FAILED, False):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
