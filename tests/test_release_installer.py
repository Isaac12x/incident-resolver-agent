from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

INSTALLER = Path(__file__).parents[1] / "install.sh"


def _fake_bin(
    tmp_path: Path, *, release: dict | None = None, curl_failure: str = ""
) -> tuple[Path, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "commands.log"
    payload = json.dumps(release or {"assets": []})
    (bindir / "curl").write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {log}\n"
        f"if [ -n '{curl_failure}' ]; then exit 22; fi\n"
        "out=\n"
        "previous=\n"
        "for arg in \"$@\"; do\n"
        "  if [ \"$previous\" = -o ]; then out=$arg; fi\n"
        "  previous=$arg\n"
        "done\n"
        f"if [ -n \"$out\" ]; then printf '%s' '{payload}' > \"$out\"; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (bindir / "uv").write_text(
        "#!/bin/sh\n"
        f"printf 'uv %s\\n' \"$*\" >> {log}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    for command in (bindir / "curl", bindir / "uv"):
        command.chmod(command.stat().st_mode | stat.S_IXUSR)
    return bindir, log


def _run_installer(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], str]:
    bindir, log = _fake_bin(tmp_path, **env.pop("_fake", {}))
    process_env = {**os.environ, **env, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    result = subprocess.run(
        ["sh", str(INSTALLER)], capture_output=True, text=True, env=process_env, check=False
    )
    return result, log.read_text(encoding="utf-8")


def test_installer_resolves_release_wheel_and_invokes_uv(tmp_path: Path) -> None:
    release = {
        "assets": [
            {
                "name": "incident_harness-0.2.0-py3-none-any.whl",
                "browser_download_url": "https://example.test/incident_harness-0.2.0-py3-none-any.whl",
            }
        ]
    }
    result, log = _run_installer(tmp_path, _fake={"release": release})
    assert result.returncode == 0
    assert "releases/latest" in log
    assert "uv tool install --force --from" in log
    assert "incident_harness-0.2.0-py3-none-any.whl" in log


def test_installer_source_override_skips_release_lookup(tmp_path: Path) -> None:
    result, log = _run_installer(
        tmp_path,
        INCIDENT_HARNESS_SOURCE="git+https://example.test/fork.git@main",
    )
    assert result.returncode == 0
    assert "releases/latest" not in log
    assert "git+https://example.test/fork.git@main" in log


def test_installer_fails_when_release_has_no_wheel(tmp_path: Path) -> None:
    result, log = _run_installer(tmp_path, _fake={"release": {"assets": []}})
    assert result.returncode != 0
    assert "does not contain a wheel" in result.stderr
    assert "uv tool install" not in log


def test_installer_fails_when_release_metadata_download_fails(tmp_path: Path) -> None:
    result, log = _run_installer(tmp_path, _fake={"curl_failure": "yes"})
    assert result.returncode != 0
    assert "Could not resolve release metadata" in result.stderr
    assert "uv tool install" not in log


def test_installer_rejects_checksum_mismatch(tmp_path: Path) -> None:
    release = {
        "assets": [
            {
                "name": "incident_harness-0.2.0-py3-none-any.whl",
                "browser_download_url": "https://example.test/incident_harness-0.2.0-py3-none-any.whl",
            }
        ]
    }
    result, log = _run_installer(
        tmp_path,
        INCIDENT_HARNESS_SHA256="0000000000000000000000000000000000000000000000000000000000000000",
        _fake={"release": release},
    )
    assert result.returncode != 0
    assert "checksum verification failed" in result.stderr
    assert "uv tool install" not in log
