import asyncio
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adaptive import AdaptiveToolRouter, ToolPolicy
from src.agent import AgentRunContext, OpenAIAgentsBackend, SubscriptionCLIBackend
from src.config import Config
from src.extensions import RegistryError, ToolManifest, TrustedToolRegistry
from src.tools import WorkspaceTools


def test_policy_persists_and_learns_preferred_tool(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    policy = ToolPolicy(path, exploration=0, seed=1)
    assert policy.choose("timeouts", ["slow", "fast"]) == "slow"
    for _ in range(4):
        policy.record("timeouts", "fast", 1)
    policy.record("timeouts", "slow", -1)
    reloaded = ToolPolicy(path, exploration=0, seed=1)
    assert reloaded.choose("timeouts", ["slow", "fast"]) == "fast"


@pytest.mark.asyncio
async def test_router_retries_and_records_exhaustion(tmp_path: Path) -> None:
    calls = 0

    async def flaky(_args):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("temporary")
        return {"ok": True}

    router = AdaptiveToolRouter(
        ToolPolicy(tmp_path / "p.json", exploration=0),
        {"tool": flaky},
        backoff=0,
        retryable_tools={"tool"},
    )
    result = await router.invoke("ctx")
    assert result.success and result.attempts == 3 and calls == 3
    assert router.policy.snapshot()["ctx"]["tool"]["reward"] == 1


def test_registry_requires_pinned_digest_and_permission(tmp_path: Path) -> None:
    source = tmp_path / "tool.whl"
    source.write_bytes(b"package")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = ToolManifest.from_dict(
        {
            "name": "demo",
            "version": "1.0",
            "source": str(source),
            "sha256": digest,
            "entrypoint": "demo:main",
        }
    )
    registry = TrustedToolRegistry([manifest])
    assert registry.catalog()[0]["loaded"] is False
    with pytest.raises(PermissionError):
        registry.install("demo", tmp_path / "installed")
    with pytest.raises(RegistryError):
        ToolManifest.from_dict(
            {
                "name": "x",
                "version": "1",
                "source": str(source),
                "sha256": "bad",
                "entrypoint": "x:y",
            }
        )


def test_registry_local_and_mcp_catalog() -> None:
    registry = TrustedToolRegistry()
    registry.register_local("custom", lambda _: "ok")

    class MCP:
        async def list_tools(self):
            return []

        async def call_tool(self, _name, _args):
            return None

    registry.register_mcp("remote", MCP())
    assert {item["name"] for item in registry.catalog()} == {"custom", "remote"}


def test_install_loads_and_executes_pinned_wheel(tmp_path: Path) -> None:
    wheel = tmp_path / "demo_tool-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("demo_tool.py", "def run(args): return {'answer': args['value'] + 1}\n")
        archive.writestr(
            "demo_tool-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: demo-tool\nVersion: 1.0\n",
        )
        archive.writestr(
            "demo_tool-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr("demo_tool-1.0.dist-info/RECORD", "")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    manifest = ToolManifest.from_dict(
        {
            "name": "demo-tool",
            "version": "1.0",
            "source": str(wheel),
            "sha256": digest,
            "entrypoint": "demo_tool:run",
        }
    )
    registry = TrustedToolRegistry([manifest])
    registry.install("demo-tool", tmp_path / "installed", allow_install=True)
    assert registry.catalog()[0]["loaded"]
    assert registry.loaded["demo-tool"]({"value": 2}) == {"answer": 3}
    restored = TrustedToolRegistry([manifest])
    restored.restore(tmp_path / "installed")
    assert restored.loaded["demo-tool"]({"value": 4}) == {"answer": 5}


def test_registry_rejects_invalid_manifests_and_catalog_files(tmp_path: Path) -> None:
    source = tmp_path / "x.whl"
    source.write_bytes(b"x")
    base = {
        "name": "ok",
        "version": "1",
        "source": str(source),
        "sha256": hashlib.sha256(b"x").hexdigest(),
        "entrypoint": "m:f",
    }
    for key, value in (
        ("name", "../x"),
        ("version", "../x"),
        ("entrypoint", "bad"),
        ("source", str(tmp_path / "missing")),
        ("sha256", "bad"),
    ):
        invalid = dict(base)
        invalid[key] = value
        with pytest.raises(RegistryError):
            ToolManifest.from_dict(invalid)
    (tmp_path / "bad.json").write_text(json.dumps({"tools": {}}))
    with pytest.raises(RegistryError):
        TrustedToolRegistry.from_file(tmp_path / "bad.json")


def test_registry_failure_paths_and_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "x.whl"
    source.write_bytes(b"x")
    digest = hashlib.sha256(b"x").hexdigest()
    manifest = ToolManifest("x", "1", str(source), digest, "m:f")
    registry = TrustedToolRegistry([manifest])
    with pytest.raises(RegistryError):
        registry.install("missing", tmp_path, allow_install=True)
    monkeypatch.setattr(registry, "_source_bytes", lambda _source: b"wrong")
    with pytest.raises(RegistryError):
        registry.install("x", tmp_path, allow_install=True)
    with pytest.raises(RegistryError):
        registry.load("missing")
    with pytest.raises(RegistryError):
        registry.register_mcp("bad", object())
    registry.register_local("local", object())
    with pytest.raises(RegistryError):
        registry.register_local("local", object())
    with pytest.raises(RegistryError):
        registry.load("x")
    malformed = tmp_path / "installed" / "bad" / "manifest.json"
    malformed.parent.mkdir(parents=True)
    malformed.write_text("not-json")
    registry.restore(tmp_path / "installed")


def test_registry_rejects_unsupported_and_nonwheel(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("x")
    digest = hashlib.sha256(b"x").hexdigest()
    unsupported = ToolManifest("x", "1", str(source), digest, "m:f", kind="shell")
    with pytest.raises(RegistryError):
        TrustedToolRegistry([unsupported]).install("x", tmp_path, allow_install=True)
    python_manifest = ToolManifest("x", "1", str(source), digest, "m:f")
    with pytest.raises(RegistryError):
        TrustedToolRegistry([python_manifest]).install("x", tmp_path, allow_install=True)
    (tmp_path / "registry.json").write_text(json.dumps({"tools": []}))
    assert TrustedToolRegistry.from_file(tmp_path / "registry.json").catalog() == []


def test_source_size_and_restore_invalid_record(tmp_path: Path) -> None:
    source = tmp_path / "x.whl"
    source.write_bytes(b"xx")
    with pytest.raises(RegistryError):
        TrustedToolRegistry._source_bytes(str(source), maximum=1)


@pytest.mark.asyncio
async def test_sdk_adaptive_descriptor_invokes_real_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    (workspace / "answer.txt").write_text("ok")

    class Agent:
        def __init__(self, **kwargs):
            self.tools = kwargs["tools"]

    class Runner:
        @staticmethod
        async def run(agent, prompt, max_turns, session):
            adaptive = next(
                item for item in agent.tools if getattr(item, "__name__", "") == "adaptive_tool"
            )
            result = json.loads(await adaptive("sdk", {"path": "answer.txt"}, tool="read_file"))
            assert result["success"] is True
            return SimpleNamespace(final_output={"ok": True})

    fake = SimpleNamespace(
        Agent=Agent,
        Runner=Runner,
        SQLiteSession=lambda *a, **k: object(),
        ModelSettings=lambda **k: SimpleNamespace(**k),
        function_tool=lambda fn: fn,
    )
    monkeypatch.setitem(sys.modules, "agents", fake)
    context = AgentRunContext(
        SimpleNamespace(),
        "sdk",
        tmp_path / "sessions.sqlite3",
        SimpleNamespace(),
        lambda _: None,
        lambda _: None,
        adaptive_router=None,
    )
    config = Config()
    config.model.compaction_enabled = False
    config.agent.max_subagents = 0
    result = await OpenAIAgentsBackend(config)(
        "i", "p", WorkspaceTools(workspace), [], run_context=context
    )
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_sdk_catalog_install_and_failed_shell_are_exposed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()

    class Registry:
        loaded = {"custom": lambda args: {"value": args["value"]}}

        def catalog(self):
            return [{"name": "custom", "loaded": True}]

        def install(self, name, target, allow_install=False):
            assert allow_install is True
            return target / name

    class Agent:
        def __init__(self, **kwargs):
            self.tools = kwargs["tools"]

    class Runner:
        @staticmethod
        async def run(agent, prompt, max_turns, session):
            names = {getattr(item, "__name__", "") for item in agent.tools}
            assert {"adaptive_tool", "tool_catalog", "tool_install"}.issubset(names)
            adaptive = next(
                item for item in agent.tools if getattr(item, "__name__", "") == "adaptive_tool"
            )
            failed = json.loads(await adaptive("fail", {"command": "false"}, tool="shell"))
            assert failed["success"] is False and failed["reward"] < 0
            catalog = next(
                item for item in agent.tools if getattr(item, "__name__", "") == "tool_catalog"
            )
            assert json.loads(catalog())[0]["name"] == "custom"
            install = next(
                item for item in agent.tools if getattr(item, "__name__", "") == "tool_install"
            )
            assert "installed" in json.loads(install("custom"))
            return SimpleNamespace(final_output={"ok": True})

    fake = SimpleNamespace(
        Agent=Agent,
        Runner=Runner,
        SQLiteSession=lambda *a, **k: object(),
        ModelSettings=lambda **k: SimpleNamespace(**k),
        function_tool=lambda fn: fn,
    )
    monkeypatch.setitem(sys.modules, "agents", fake)
    config = Config()
    config.model.compaction_enabled = False
    config.agent.max_subagents = 0
    context = AgentRunContext(
        SimpleNamespace(),
        "sdk2",
        tmp_path / "sessions.sqlite3",
        SimpleNamespace(),
        lambda _: None,
        lambda _: None,
        tool_registry=Registry(),
    )
    result = await OpenAIAgentsBackend(config)(
        "i", "p", WorkspaceTools(workspace), [], run_context=context
    )
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_subscription_bridge_catalog_install_adaptive_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    (workspace / "answer.txt").write_text("ok")

    class Registry:
        loaded = {}

        def catalog(self):
            return [{"name": "demo"}]

        def install(self, name, target, allow_install=False):
            assert allow_install
            return target / name

    async def lifecycle(**kwargs):
        return {"ok": True}

    context = AgentRunContext(
        SimpleNamespace(),
        "bridge",
        tmp_path / "sessions.sqlite3",
        SimpleNamespace(
            **{
                name: lifecycle
                for name in (
                    "mark_investigation_complete",
                    "run_tests",
                    "verification_plan",
                    "open_pr",
                    "remember",
                )
            }
        ),
        lambda _: None,
        lambda _: None,
        tool_registry=Registry(),
    )
    config = Config()
    backend = SubscriptionCLIBackend(config)
    async with backend._tool_bridge(context, WorkspaceTools(workspace)) as launcher:

        async def call(name, args):
            result = await asyncio.to_thread(
                subprocess.run,
                [str(launcher), name, json.dumps(args)],
                capture_output=True,
                text=True,
                check=False,
            )
            return json.loads(result.stdout)

        assert (await call("tool_catalog", {}))["ok"]
        assert (await call("tool_install", {"name": "demo"}))["ok"]
        assert (
            await call(
                "adaptive_tool",
                {"context": "bridge", "tool": "read_file", "arguments": {"path": "answer.txt"}},
            )
        )["ok"]
        failed = await call(
            "adaptive_tool",
            {"context": "bridge", "tool": "shell", "arguments": {"command": "false"}},
        )
        assert failed["ok"]
