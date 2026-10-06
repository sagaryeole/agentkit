"""Opt-in tests against real model servers. Skipped when the server or key is absent.

uv run pytest tests/integration -m local_model     # needs LOCAL_MODEL_ID and a running server
uv run pytest tests/integration -m integration     # needs ANTHROPIC_API_KEY
"""

import asyncio
import os
from pathlib import Path

import pytest

from agentkit.config import load_config
from agentkit.errors import ModelError
from agentkit.models import Message, ModelRequest, get_model
from agentkit.models.providers.local import list_local_models
from agentkit.models.registry import local_api_base
from examples.single_agent.agent import run

REQUEST = ModelRequest(messages=[Message.from_text("user", "Reply with the single word: ok")])


@pytest.fixture
def local_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MODEL_PROVIDER", "local")
    cfg = load_config(tmp_path)
    try:
        asyncio.run(list_local_models(local_api_base(cfg), server=cfg.models.local.server))
    except ModelError as exc:
        pytest.skip(str(exc))
    if not os.environ.get("LOCAL_MODEL_ID"):
        pytest.skip("LOCAL_MODEL_ID is not set")
    return cfg


@pytest.mark.local_model
async def test_local_model_answers_and_costs_nothing(local_config) -> None:  # type: ignore[no-untyped-def]
    response = await get_model("default", local_config).generate(REQUEST)
    assert response.message.text
    assert response.usage.cost_usd == 0


@pytest.mark.local_model
async def test_local_model_streams(local_config) -> None:  # type: ignore[no-untyped-def]
    events = [event async for event in get_model("default", local_config).stream(REQUEST)]
    assert events[-1].type == "done" and events[-1].response is not None
    assert "".join(e.text for e in events[:-1]) == events[-1].response.message.text


@pytest.mark.local_model
async def test_example_agent_uses_its_tool_on_a_local_model(local_config) -> None:  # type: ignore[no-untyped-def]
    assert await run("What is 1717 times 2323?", local_config)


@pytest.mark.integration
@pytest.mark.skipif(
    not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
    reason="no Anthropic credentials in the environment",
)
async def test_anthropic_answers_counts_tokens_and_is_priced(tmp_path: Path) -> None:
    model = get_model("fast", load_config(tmp_path), environ={})
    response = await model.generate(REQUEST)
    assert "ok" in response.message.text.lower()
    assert response.usage.cost_usd is not None and response.usage.cost_usd > 0
    assert await model.count_tokens(REQUEST) > 0
