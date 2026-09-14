from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adaptive import AdaptiveToolRouter, ToolPolicy
from src.extensions import RegistryError, ToolManifest, TrustedToolRegistry


@pytest.mark.asyncio
async def test_candidates_are_explicit_and_failures_never_earn_positive_rewards(tmp_path):
    policy = ToolPolicy(tmp_path / "policy.json")
    router = AdaptiveToolRouter(
        policy, {"shell": lambda _: {"returncode": 1}, "read": lambda _: {"content": "ok"}}
    )
    for candidates in (None, [], ["untrusted"]):
        with pytest.raises(ValueError):
            await router.invoke("ctx", candidates=candidates)
    with pytest.raises(ValueError):
        await router.invoke("ctx", tool="shell", candidates=["read"])
    with pytest.raises(KeyError):
        await router.invoke("ctx", tool="missing")
    failure = await router.invoke("ctx", tool="shell")
    assert not failure.success and failure.reward == -1 and failure.attempts == 1
    assert (await router.invoke("ctx", candidates=["read"])).success
    router.tools["connector"] = lambda _: SimpleNamespace(isError=True)
    assert not (await router.invoke("ctx", tool="connector")).success
    router.tools["invalid"] = lambda _: 1 / 0
    assert not (await router.invoke("ctx", tool="invalid")).success


def test_installed_tool_failure_is_explicit(tmp_path, monkeypatch):
    manifest = ToolManifest("tool", "1", "/unused.whl", "0" * 64, "module:run")
    monkeypatch.setattr(
        "src.extensions.run_bounded_json",
        lambda *a, **kw: {"available": False, "reason": "TimeoutExpired"},
    )
    invoke = TrustedToolRegistry._installed_tool(tmp_path, manifest)
    with pytest.raises(RegistryError, match="TimeoutExpired"):
        invoke({})


def test_policy_updates_from_independent_processes_are_not_lost(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "policy.json"
    script = (
        "from src.adaptive import ToolPolicy; import sys; "
        "p=ToolPolicy(sys.argv[1]); "
        '[p.record("context", "tool", 1) for _ in range(10)]'
    )
    children = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(path)], cwd=Path(__file__).resolve().parents[1]
        )
        for _ in range(3)
    ]
    assert all(child.wait(timeout=20) == 0 for child in children)
    assert ToolPolicy(path).snapshot()["context"]["tool"] == {"count": 30, "reward": 30}
