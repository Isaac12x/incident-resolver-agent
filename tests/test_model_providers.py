"""Tests for extensible model provider registration."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from src.agent import OpenAIAgentsBackend
from src.config import Config, ModelConfig
from src.model_providers import get_model_provider, register_model_provider


def test_register_and_lookup_model_provider() -> None:
    def factory(config: ModelConfig) -> object:
        return config.name

    register_model_provider("test-model", factory)
    register_model_provider("TEST-MODEL", factory)

    assert get_model_provider(" TEST-MODEL ") is factory
    assert get_model_provider("missing-model") is None


def test_model_provider_registration_rejects_invalid_and_duplicate_names() -> None:
    with pytest.raises(ValueError, match="provider name"):
        register_model_provider("bad/name", lambda config: None)
    register_model_provider("duplicate-model", lambda config: None)
    with pytest.raises(ValueError, match="already registered"):
        register_model_provider(" DUPLICATE-MODEL ", lambda config: None)


def test_backend_uses_registered_model_provider_and_caches_model() -> None:
    calls: list[ModelConfig] = []
    model = object()

    def factory(config: ModelConfig) -> object:
        calls.append(config)
        return model

    register_model_provider("test-backend", factory)
    config = Config(model=ModelConfig(provider="test-backend", name="special"))
    backend = OpenAIAgentsBackend(config)

    assert backend._model(SimpleNamespace()) is model  # noqa: SLF001
    assert backend._model(SimpleNamespace()) is model  # noqa: SLF001
    assert calls == [config.model]


@pytest.mark.asyncio
async def test_backend_runtime_passes_registered_model_to_agents(monkeypatch) -> None:
    model = object()
    register_model_provider("runtime-test-model", lambda config: model)
    config = Config(
        model=ModelConfig(
            provider="runtime-test-model",
            show_execution_details=False,
        )
    )
    constructed_agents: list[dict[str, object]] = []

    class FakeAgent:
        def __init__(self, **values):  # noqa: ANN003
            constructed_agents.append(values)

    class FakeRunner:
        @staticmethod
        async def run(agent, prompt, **kwargs):  # noqa: ANN001, ANN003
            return SimpleNamespace(final_output={"status": "ok"})

    agents_module = sys.modules["agents"]
    monkeypatch.setattr(agents_module, "Agent", FakeAgent)
    monkeypatch.setattr(agents_module, "ModelSettings", lambda **kwargs: kwargs)
    monkeypatch.setattr(agents_module, "Runner", FakeRunner)
    monkeypatch.setattr(agents_module, "function_tool", lambda function: function)

    result = await OpenAIAgentsBackend(config)("instructions", "prompt", SimpleNamespace(), [])

    assert result == {"status": "ok"}
    assert constructed_agents[-1]["model"] is model
