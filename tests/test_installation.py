from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock, patch

import pytest

from src.__main__ import main
from src.config import Config, load_config, save_config
from src.lifecycle import (
    bootstrap,
    default_config_path,
    default_runtime_path,
    ensure_user_config,
    update_installation,
)
from src.tooling import ToolResult


def test_default_paths_follow_xdg(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert default_config_path(tmp_path) == tmp_path / "config" / "incident-harness" / "config.toml"
    assert default_runtime_path() == tmp_path / "state" / "incident-harness"
    monkeypatch.setenv("INCIDENT_AGENT_CONFIG", "~/custom.toml")
    assert default_config_path(tmp_path) == Path("~/custom.toml").expanduser()


def test_checkout_config_wins_and_user_config_uses_state(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / ".agent").mkdir()
    assert default_config_path(tmp_path) == tmp_path / ".agent" / "config.toml"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = tmp_path / "user" / "config.toml"
    ensure_user_config(path)
    config = load_config(path)
    assert config.runtime_root == tmp_path / "state" / "incident-harness"
    assert bootstrap(tmp_path / "nested" / "config.toml") == tmp_path / "nested" / "config.toml"
    ensure_user_config(path)  # Existing config is deliberately left untouched.


def test_update_returns_actual_uv_result(monkeypatch) -> None:
    monkeypatch.setattr("src.lifecycle.shutil.which", lambda _: "/usr/bin/uv")
    monkeypatch.setattr(
        "src.lifecycle.release_asset_url",
        lambda **_: (
            "https://github.com/example/release/releases/download/v0.2.0/incident_harness-0.2.0-py3-none-any.whl"
        ),
    )
    expected = CompletedProcess(["uv"], 0, "updated\n", "")
    result = update_installation(runner=lambda *args, **kwargs: expected)
    assert result is expected


def test_cli_update_reports_stages_without_installer_noise(capsys) -> None:
    def update(**kwargs):
        kwargs["progress"]("incident-agent")
        assert "Updating incident-agent" in capsys.readouterr().err
        kwargs["progress"]("seed")
        assert "Updating seed-cli" in capsys.readouterr().err
        return CompletedProcess(["uv"], 0, "Resolved 8 packages", "Installed 8 packages")

    with patch("src.__main__.update_installation", side_effect=update):
        main(["update", "nightly"])

    captured = capsys.readouterr()
    assert captured.out == "Update complete.\n"
    assert captured.err == ""


def test_update_reinstalls_managed_tools_and_reports_progress(monkeypatch) -> None:
    monkeypatch.setattr("src.lifecycle.shutil.which", lambda _: "/usr/bin/uv")
    commands = []
    progress = []

    def runner(command, **kwargs):
        commands.append(command)
        return CompletedProcess(command, 0, "installed", "")

    update_installation(
        managed_tools=("seed",), runner=runner, progress=progress.append
    )

    assert commands[1] == [
        "/usr/bin/uv",
        "tool",
        "install",
        "--force",
        "--upgrade",
        "seed-cli",
    ]
    assert progress == ["incident-agent", "seed"]


def test_installation_source_resolves_release_pins(monkeypatch) -> None:
    from src.lifecycle import installation_source

    monkeypatch.delenv("INCIDENT_HARNESS_SOURCE", raising=False)
    monkeypatch.setenv("INCIDENT_HARNESS_VERSION", "v0.3.0")
    monkeypatch.setattr(
        "src.lifecycle.release_asset_url",
        lambda **kwargs: f"https://github.com/{kwargs['repository']}/{kwargs['version']}.whl",
    )
    assert installation_source() == "https://github.com/Isaac12x/incident-resolver-agent/v0.3.0.whl"


def test_installation_source_supports_pinned_release_and_fork(monkeypatch) -> None:
    from src.lifecycle import installation_source

    monkeypatch.setenv("INCIDENT_HARNESS_REPOSITORY", "example/fork")
    monkeypatch.setenv("INCIDENT_HARNESS_VERSION", "v0.2.0")
    monkeypatch.setattr(
        "src.lifecycle.release_asset_url",
        lambda **kwargs: (
            f"https://github.com/{kwargs['repository']}/releases/download/{kwargs['version']}/"
            "incident_harness-0.2.0-py3-none-any.whl"
        ),
    )
    assert installation_source().endswith(
        "example/fork/releases/download/v0.2.0/incident_harness-0.2.0-py3-none-any.whl"
    )
    monkeypatch.setenv("INCIDENT_HARNESS_SOURCE", "git+https://example/fork.git@dev")
    assert installation_source() == "git+https://example/fork.git@dev"


def test_installed_release_repository_reads_direct_url(monkeypatch) -> None:
    from src.lifecycle import installed_release_repository

    class Metadata:
        def read_text(self, name):
            assert name == "direct_url.json"
            return '{"url":"https://github.com/example/fork/releases/download/v0.2.0/incident_harness-0.2.0-py3-none-any.whl"}'

    monkeypatch.setattr("src.lifecycle.distribution", lambda _: Metadata())
    assert installed_release_repository() == "example/fork"


def test_unknown_installed_source_preserves_upgrade_target_and_refreshes(monkeypatch) -> None:
    from src.lifecycle import update_installation

    monkeypatch.setattr("src.lifecycle.shutil.which", lambda _: "/usr/bin/uv")
    monkeypatch.setattr("src.lifecycle.installed_release_repository", lambda: None)
    monkeypatch.setattr("src.lifecycle.installed_git_repository", lambda: None)
    commands = []
    update_installation(
        runner=lambda command, **kwargs: (
            commands.append(command) or CompletedProcess(command, 0, "updated", "")
        )
    )
    assert commands[0] == [
        "/usr/bin/uv",
        "tool",
        "upgrade",
        "--reinstall-package",
        "incident-harness",
        "incident-harness",
    ]


def test_default_update_tracks_default_branch_and_refreshes(monkeypatch) -> None:
    monkeypatch.setattr("src.lifecycle.shutil.which", lambda _: "/usr/bin/uv")
    monkeypatch.setattr(
        "src.lifecycle.installed_git_repository", lambda: "Isaac12x/incident-resolver-agent"
    )
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        return CompletedProcess(command, 0, "updated", "")

    update_installation(runner=runner)
    update_installation(runner=runner)
    assert commands[0] == [
        "/usr/bin/uv",
        "tool",
        "install",
        "--refresh",
        "--upgrade",
        "--from",
        "git+https://github.com/Isaac12x/incident-resolver-agent.git",
        "incident-harness",
    ]
    assert commands[1] == commands[0]


@pytest.mark.skipif(not shutil.which("uv") or not shutil.which("git"), reason="uv and git required")
def test_default_update_refreshes_same_version_git_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = tmp_path / "package"
    repository.mkdir()
    (repository / "pyproject.toml").write_text(
        "[build-system]\nrequires = []\nbuild-backend = 'backend'\nbackend-path = ['.']\n"
        "[project]\nname = 'incident-harness'\nversion = '0.1.0'\n"
        "[project.scripts]\nincident-agent = 'incident_harness:main'\n",
        encoding="utf-8",
    )
    (repository / "backend.py").write_text(
        """from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

NAME = 'incident_harness-0.1.0-py3-none-any.whl'

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    files = {
        'incident_harness.py': Path(__file__).with_name('incident_harness.py').read_text(),
        'incident_harness-0.1.0.dist-info/METADATA': (
            'Metadata-Version: 2.1\\nName: incident-harness\\nVersion: 0.1.0\\n'
        ),
        'incident_harness-0.1.0.dist-info/WHEEL': (
            'Wheel-Version: 1.0\\nGenerator: local-test\\nRoot-Is-Purelib: true\\n'
            'Tag: py3-none-any\\n'
        ),
        'incident_harness-0.1.0.dist-info/entry_points.txt': (
            '[console_scripts]\\nincident-agent = incident_harness:main\\n'
        ),
    }
    path = Path(wheel_directory) / NAME
    with ZipFile(path, 'w', ZIP_DEFLATED) as wheel:
        for name, content in files.items():
            wheel.writestr(name, content)
        wheel.writestr('incident_harness-0.1.0.dist-info/RECORD', '')
    return NAME
""",
        encoding="utf-8",
    )
    module = repository / "incident_harness.py"
    module.write_text("def main(): print('before')\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.test"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "before"], check=True)

    tool_dir = tmp_path / "uv-tools"
    tool_bin = tmp_path / "uv-bin"
    monkeypatch.setenv("UV_TOOL_DIR", str(tool_dir))
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(tool_bin))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    monkeypatch.setenv("INCIDENT_HARNESS_SOURCE", f"git+file://{repository}")
    first = update_installation()
    assert first.returncode == 0, first.stderr
    executable = tool_bin / "incident-agent"
    assert subprocess.check_output([str(executable)], text=True).strip() == "before"

    module.write_text("def main(): print('after')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "after"], check=True)
    second = update_installation()
    assert second.returncode == 0, second.stderr
    assert subprocess.check_output([str(executable)], text=True).strip() == "after"

    module.write_text("def main(): print('custom')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "custom"], check=True)
    monkeypatch.delenv("INCIDENT_HARNESS_SOURCE")
    monkeypatch.setattr("src.lifecycle.installed_git_repository", lambda: None)
    monkeypatch.setattr("src.lifecycle.installed_release_repository", lambda: None)
    third = update_installation()
    assert third.returncode == 0, third.stderr
    assert subprocess.check_output([str(executable)], text=True).strip() == "custom"


def test_bare_update_honors_explicit_release_pin(monkeypatch) -> None:
    monkeypatch.setattr("src.lifecycle.shutil.which", lambda _: "/usr/bin/uv")
    monkeypatch.setenv("INCIDENT_HARNESS_VERSION", "v0.3.0")
    monkeypatch.setenv("INCIDENT_HARNESS_REPOSITORY", "example/fork")
    monkeypatch.setattr(
        "src.lifecycle.release_asset_url",
        lambda **kwargs: f"https://example/{kwargs['repository']}/{kwargs['version']}.whl",
    )
    commands = []
    update_installation(
        runner=lambda command, **kwargs: (
            commands.append(command) or CompletedProcess(command, 0, "updated", "")
        ),
    )
    assert commands[0][-2:] == ["https://example/example/fork/v0.3.0.whl", "incident-harness"]


def test_stable_update_returns_default_git_install_to_release(monkeypatch) -> None:
    monkeypatch.setattr("src.lifecycle.shutil.which", lambda _: "/usr/bin/uv")
    monkeypatch.setattr("src.lifecycle.installed_release_repository", lambda: None)
    monkeypatch.setattr(
        "src.lifecycle.installed_git_repository",
        lambda: "Isaac12x/incident-resolver-agent",
    )
    monkeypatch.setattr(
        "src.lifecycle.release_asset_url", lambda **_: "https://example/release.whl"
    )
    commands = []
    update_installation(
        channel="stable",
        runner=lambda command, **kwargs: (
            commands.append(command) or CompletedProcess(command, 0, "updated", "")
        ),
    )
    assert commands[0] == [
        "/usr/bin/uv",
        "tool",
        "install",
        "--force",
        "--from",
        "https://example/release.whl",
        "incident-harness",
    ]


def test_fork_default_update_preserves_repository_and_stable_returns_to_release(
    monkeypatch,
) -> None:
    monkeypatch.setattr("src.lifecycle.shutil.which", lambda _: "/usr/bin/uv")
    monkeypatch.setattr("src.lifecycle.installed_git_repository", lambda: "example/fork")
    monkeypatch.setattr("src.lifecycle.installed_release_repository", lambda: None)
    monkeypatch.setattr(
        "src.lifecycle.release_asset_url",
        lambda **kwargs: f"https://example/{kwargs['repository']}.whl",
    )
    commands = []
    update_installation(
        channel="stable",
        runner=lambda command, **kwargs: (
            commands.append(command) or CompletedProcess(command, 0, "updated", "")
        ),
    )
    assert commands[0][-2:] == ["https://example/example/fork.whl", "incident-harness"]
    commands.clear()
    update_installation(
        channel="nightly",
        runner=lambda command, **kwargs: (
            commands.append(command) or CompletedProcess(command, 0, "updated", "")
        ),
    )
    assert "git+https://github.com/example/fork.git" in commands[0]


def test_nightly_from_fork_release_uses_release_provenance(monkeypatch) -> None:
    monkeypatch.setattr("src.lifecycle.shutil.which", lambda _: "/usr/bin/uv")
    monkeypatch.setattr("src.lifecycle.installed_git_repository", lambda: None)
    monkeypatch.setattr("src.lifecycle.installed_release_repository", lambda: "example/fork")
    commands = []
    update_installation(
        channel="nightly",
        runner=lambda command, **kwargs: (
            commands.append(command) or CompletedProcess(command, 0, "updated", "")
        ),
    )
    assert "git+https://github.com/example/fork.git" in commands[0]


def test_nightly_rejects_release_only_overrides(monkeypatch) -> None:
    monkeypatch.setattr("src.lifecycle.shutil.which", lambda _: "/usr/bin/uv")
    monkeypatch.setenv("INCIDENT_HARNESS_VERSION", "v0.3.0")
    with pytest.raises(RuntimeError, match="only apply to stable"):
        update_installation(channel="nightly")


def test_nightly_update_preserves_explicit_source(monkeypatch) -> None:
    monkeypatch.setattr("src.lifecycle.shutil.which", lambda _: "/usr/bin/uv")
    monkeypatch.setenv("INCIDENT_HARNESS_SOURCE", "git+https://example.test/fork.git")
    commands = []
    update_installation(
        channel="nightly",
        runner=lambda command, **kwargs: (
            commands.append(command) or CompletedProcess(command, 0, "updated", "")
        ),
    )
    assert commands[0][-2:] == ["git+https://example.test/fork.git", "incident-harness"]


def test_nightly_update_uses_explicit_repository(monkeypatch) -> None:
    monkeypatch.setattr("src.lifecycle.shutil.which", lambda _: "/usr/bin/uv")
    monkeypatch.setenv("INCIDENT_HARNESS_REPOSITORY", "example/nightly-fork")
    commands = []
    update_installation(
        channel="nightly",
        runner=lambda command, **kwargs: (
            commands.append(command) or CompletedProcess(command, 0, "updated", "")
        ),
    )
    assert "git+https://github.com/example/nightly-fork.git" in commands[0]


def test_update_rejects_unknown_channel(monkeypatch) -> None:
    monkeypatch.setattr("src.lifecycle.shutil.which", lambda _: "/usr/bin/uv")
    with pytest.raises(ValueError, match="unsupported update channel"):
        update_installation(channel="preview")


def test_nightly_failure_skips_managed_tool_upgrades(monkeypatch) -> None:
    monkeypatch.setattr("src.lifecycle.shutil.which", lambda _: "/usr/bin/uv")
    commands = []
    result = update_installation(
        channel="nightly",
        managed_tools=("seed",),
        runner=lambda command, **kwargs: (
            commands.append(command) or CompletedProcess(command, 7, "", "failed")
        ),
    )
    assert result.returncode == 7
    assert len(commands) == 1


def test_release_asset_url_selects_project_wheel() -> None:
    import io

    from src.lifecycle import release_asset_url

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    response = Response(
        b'{"assets":[{"name":"other.whl","browser_download_url":"https://example/other.whl"},'
        b'{"name":"incident_harness-0.2.0-py3-none-any.whl",'
        b'"browser_download_url":"https://example/incident_harness-0.2.0-py3-none-any.whl"}]}'
    )
    assert release_asset_url(opener=lambda *_args, **_kwargs: response).endswith(
        "incident_harness-0.2.0-py3-none-any.whl"
    )


@pytest.mark.parametrize("payload", [b"not-json", b'{"assets": []}'])
def test_release_asset_url_rejects_bad_or_missing_metadata(payload: bytes) -> None:
    from src.lifecycle import release_asset_url

    class Response:
        def __enter__(self):
            import io

            return io.BytesIO(payload)

        def __exit__(self, *_):
            return None

    with pytest.raises(RuntimeError, match="release|metadata"):
        release_asset_url(opener=lambda *_args, **_kwargs: Response())


def test_release_asset_url_supports_tag_and_exact_asset() -> None:
    import io

    from src.lifecycle import release_asset_url

    class Response:
        def __enter__(self):
            return io.BytesIO(
                b'{"assets":[{"name":"custom.whl","browser_download_url":"https://example/custom.whl"}]}'
            )

        def __exit__(self, *_):
            return None

    calls = []

    def opener(url, **kwargs):
        calls.append(url)
        return Response()

    assert (
        release_asset_url(
            repository="example/fork", version="v1.0.0", asset="custom.whl", opener=opener
        )
        == "https://example/custom.whl"
    )
    assert calls == ["https://api.github.com/repos/example/fork/releases/tags/v1.0.0"]


def test_installed_release_repository_rejects_non_release_sources(monkeypatch) -> None:
    from src.lifecycle import installed_release_repository

    class Metadata:
        def __init__(self, value):
            self.value = value

        def read_text(self, _):
            return self.value

    for value in (
        None,
        "not-json",
        '{"url":"https://github.com/example/fork.git"}',
        '{"url":"https://example.test/releases/download/v1/wheel.whl"}',
    ):
        monkeypatch.setattr("src.lifecycle.distribution", lambda _, value=value: Metadata(value))
        assert installed_release_repository() is None


def test_installed_git_repository_reads_github_source(monkeypatch) -> None:
    from src.lifecycle import installed_git_repository

    class Metadata:
        def read_text(self, _):
            return '{"url":"git+https://github.com/example/fork.git","vcs_info":{"vcs":"git"}}'

    monkeypatch.setattr("src.lifecycle.distribution", lambda _: Metadata())
    assert installed_git_repository() == "example/fork"


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        "",
        "not-json",
        '{"url":"https://github.com/example/fork/releases/latest/wheel.whl"}',
        '{"url":"https://example.test/fork.git"}',
    ],
)
def test_installed_git_repository_rejects_missing_or_non_github_metadata(
    monkeypatch, metadata
) -> None:
    from src.lifecycle import installed_git_repository

    class Metadata:
        def read_text(self, _):
            if metadata is None:
                raise OSError("metadata unavailable")
            return metadata

    monkeypatch.setattr("src.lifecycle.distribution", lambda _: Metadata())
    assert installed_git_repository() is None


def test_update_requires_uv(monkeypatch) -> None:
    monkeypatch.setattr("src.lifecycle.shutil.which", lambda _: None)
    try:
        update_installation()
    except RuntimeError as error:
        assert "uv is required" in str(error)
    else:
        raise AssertionError("missing uv must fail")


def test_cli_eval_update_and_argument_free_run(tmp_path: Path, capsys) -> None:
    report = {"failed": 0, "total": 1}
    with patch("src.evals.run_evaluations", return_value=report):
        main(["eval"])
    assert '"total": 1' in capsys.readouterr().out
    output = tmp_path / "report.json"
    with patch("src.evals.run_evaluations", return_value=report):
        main(["eval", "--output", str(output)])
    assert output.exists()
    with (
        patch("src.evals.run_evaluations", return_value={"failed": 1}),
        pytest.raises(SystemExit, match="1"),
    ):
        main(["eval"])
    completed = CompletedProcess(["uv"], 0, "updated\n", "warning\n")
    with patch("src.__main__.update_installation", return_value=completed):
        main(["update"])
    assert "Update complete" in capsys.readouterr().out
    with patch("src.__main__.update_installation", return_value=completed) as update:
        main(["update", "nightly"])
    assert update.call_args.kwargs["channel"] == "nightly"
    config = Config(runtime_root=tmp_path / "runtime")
    application = Mock(config=config)
    with (
        patch("src.__main__.Application.build", return_value=application),
        patch("src.__main__.create_server", return_value=Mock()),
        patch("src.__main__.uvicorn.run"),
    ):
        main(["--config", str(tmp_path / "config.toml"), "run"])


def test_cli_init_explicit_and_seed_failure(tmp_path: Path, monkeypatch) -> None:
    explicit = tmp_path / "explicit.toml"
    main(["--config", str(explicit), "init"])
    assert load_config(explicit).connectors[0].name == "grafana"
    (tmp_path / "source").mkdir()
    monkeypatch.chdir(tmp_path / "source")
    failure = ToolResult(("seed",), 4, "", "failed\n")
    with (
        patch("src.__main__.initialise_runtime_tree", return_value=failure),
        pytest.raises(SystemExit, match="4"),
    ):
        main(["init"])


def test_cli_bundle_status_doctor_and_eval_suites(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config_path = tmp_path / "config.toml"
    config = Config(runtime_root=tmp_path / "runtime", agent={"skill_directories": []})
    from src.bundles import build_bundle

    save_config(config, config_path)
    version = build_bundle(config).version
    main(["--config", str(config_path), "bundle", "list"])
    assert version in capsys.readouterr().out
    main(["--config", str(config_path), "bundle", "activate", version])
    assert version in capsys.readouterr().out
    main(["--config", str(config_path), "status"])
    assert "active bundle" in capsys.readouterr().out
    with patch("src.__main__.doctor", return_value=[]):
        main(["--config", str(config_path), "doctor"])
    report = {"failed": 0, "total": 1}
    with (
        patch("src.evals.run_holdout_evaluation", return_value={"suite": "root-cause"}),
        patch("src.evals.run_retrieval_evaluation", return_value={"suite": "retrieval"}),
        patch("src.evals.run_repair_evaluation", return_value={"suite": "repair"}),
    ):
        dataset = tmp_path / "records.jsonl"
        dataset.write_text('{"task_id":"x","root_cause":"bug"}\n', encoding="utf-8")
        main(["eval", "--suite", "repair"])
        main(["eval", "--suite", "root-cause", str(dataset)])
        main(["eval", "--suite", "retrieval", str(dataset)])
    assert report["failed"] == 0


def test_cli_eval_dataset_errors_and_update_failure(tmp_path: Path, capsys) -> None:
    with pytest.raises(SystemExit, match="requires a JSON"):
        main(["eval", "--suite", "root-cause"])
    invalid = tmp_path / "invalid.jsonl"
    invalid.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="invalid evaluation"):
        main(["eval", "--suite", "retrieval", str(invalid)])
    with (
        patch(
            "src.__main__.update_installation",
            return_value=CompletedProcess(
                ["uv", "tool", "install", "seed-cli"],
                2,
                "",
                "error: Executable already exists: seed (use --force to replace)",
            ),
        ),
        pytest.raises(SystemExit, match="2"),
    ):
        main(["update"])
    assert (
        "Update failed while updating seed-cli: uv reported an executable-name conflict. "
        "Check `uv tool list` to identify the existing owner. Details: "
        "error: Executable already exists: seed (use --force to replace)"
        in capsys.readouterr().err
    )


def test_application_uses_active_bundle_from_unrelated_directory(
    tmp_path: Path, monkeypatch
) -> None:
    from src.app import Application
    from src.bundles import activate_bundle, build_bundle

    config_path = tmp_path / "config.toml"
    base = Config(runtime_root=tmp_path / "runtime", agent={"skill_directories": []})
    save_config(base, config_path)
    effective = base.model_copy(deep=True)
    effective.model.name = "bundle-model"
    bundle = build_bundle(effective)
    activate_bundle(effective, bundle.version)
    (tmp_path / "unrelated").mkdir()
    monkeypatch.chdir(tmp_path / "unrelated")
    application = Application.build(config_path, agent_backend=Mock())
    assert application.config.model.name == "bundle-model"
    assert application.agent.skills_root == bundle.path / "skills"
