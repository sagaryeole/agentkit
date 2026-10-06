from pathlib import Path

import pytest

from agentkit.config import load_config
from agentkit.models import Message, ModelResponse, ToolCallPart, Usage
from agentkit.models.providers.fake import FakeProvider
from examples.single_agent.agent import run


async def test_example_runs_on_whichever_provider_the_role_names(tmp_path: Path) -> None:
    overrides = {"models": {"roles": {"default": "fake/default"}}}
    cfg = load_config(tmp_path, environ={}, overrides=overrides)
    assert await run("What is 17 times 23?", cfg) == "fake: What is 17 times 23?"


async def test_example_executes_the_tool_and_returns_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    call = ModelResponse(
        message=Message(
            role="assistant",
            content=[ToolCallPart(id="c1", name="multiply", arguments={"a": 17, "b": 23})],
        ),
        stop_reason="tool_use",
        usage=Usage(input_tokens=1, output_tokens=1),
        model_id="fake",
    )
    fake = FakeProvider([call, lambda req: f"The answer is {req.messages[-1].content[0].content}."])
    monkeypatch.setattr("agentkit.models.registry.build_provider", lambda *args: fake)

    assert await run("What is 17 times 23?", load_config(tmp_path, environ={})) == (
        "The answer is 391."
    )
    assert [m.role for m in fake.requests[1].messages] == ["system", "user", "assistant", "tool"]
