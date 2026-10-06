import anthropic
import httpx
import pytest
from anthropic.types import Message as AnthropicMessage

from agentkit.errors import ModelError
from agentkit.models import Message, ModelRequest, OpaquePart, ToolCallPart, ToolResultPart
from agentkit.models.base import ToolSpec
from agentkit.models.providers.anthropic import (
    AnthropicProvider,
    _translate,
    from_anthropic_message,
    to_anthropic_messages,
)

THINKING = {"type": "thinking", "thinking": "", "signature": "sig"}
RAW = AnthropicMessage.model_validate(
    {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [
            THINKING,
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "tu_1", "name": "multiply", "input": {"a": 2, "b": 3}},
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 5,
        },
    }
)
TOOL = ToolSpec(name="multiply", description="Multiply.", input_schema={"type": "object"})


def test_response_is_converted_and_thinking_is_kept_opaque() -> None:
    response = from_anthropic_message(RAW)
    assert response.stop_reason == "tool_use"
    assert response.model_id == "claude-sonnet-5-5"
    assert response.message.text == "Checking."
    assert response.message.tool_calls == [
        ToolCallPart(id="tu_1", name="multiply", arguments={"a": 2, "b": 3})
    ]
    assert response.message.content[0] == OpaquePart(provider="anthropic", data=THINKING)
    # input_tokens is the total, including cache reads and writes
    assert (response.usage.input_tokens, response.usage.cached_input_tokens) == (115, 100)
    assert response.raw is RAW


def test_a_tool_round_trip_sends_thinking_back_unchanged() -> None:
    history = [
        Message.from_text("system", "Be exact."),
        Message.from_text("user", "2 x 3?"),
        from_anthropic_message(RAW).message,
        Message(role="tool", content=[ToolResultPart(tool_call_id="tu_1", content="6")]),
    ]
    assert to_anthropic_messages(history) == [
        {"role": "user", "content": [{"type": "text", "text": "2 x 3?"}]},
        {
            "role": "assistant",
            "content": [
                THINKING,
                {"type": "text", "text": "Checking."},
                {"type": "tool_use", "id": "tu_1", "name": "multiply", "input": {"a": 2, "b": 3}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "6", "is_error": False}
            ],
        },
    ]


def test_opaque_parts_from_other_providers_are_dropped() -> None:
    message = Message(role="assistant", content=[OpaquePart(provider="other", data={"x": 1})])
    assert to_anthropic_messages([message]) == [{"role": "assistant", "content": []}]


def test_request_arguments() -> None:
    provider = AnthropicProvider("claude-sonnet-5-5")
    request = ModelRequest(
        messages=[Message.from_text("system", "Be exact."), Message.from_text("user", "hi")],
        tools=[TOOL],
        tool_choice="required",
        stop=["END"],
        response_schema={"type": "object"},
        provider_options={"output_config": {"effort": "low"}, "metadata": {"user_id": "u"}},
    )
    kwargs = provider._kwargs(request)
    assert kwargs["model"] == "claude-sonnet-5-5"
    assert kwargs["system"] == "Be exact."
    assert kwargs["max_tokens"] == 16_000
    assert kwargs["tools"] == [
        {"name": "multiply", "description": "Multiply.", "input_schema": {"type": "object"}}
    ]
    assert kwargs["tool_choice"] == {"type": "any"}
    assert kwargs["stop_sequences"] == ["END"]
    # provider_options merge into, rather than replace, what the kit set
    assert kwargs["output_config"] == {
        "format": {"type": "json_schema", "schema": {"type": "object"}},
        "effort": "low",
    }
    assert kwargs["metadata"] == {"user_id": "u"}
    assert "temperature" not in kwargs

    plain = provider._kwargs(ModelRequest(messages=[Message.from_text("user", "hi")]))
    assert set(plain) == {"model", "messages", "max_tokens"}


@pytest.mark.parametrize(
    ("status", "retryable"), [(400, False), (401, False), (404, False), (429, True), (529, True)]
)
def test_errors_are_classified(status: int, retryable: bool) -> None:
    response = httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com"))
    error = anthropic.APIStatusError("nope", response=response, body=None)
    if status == 401:
        error = anthropic.AuthenticationError("nope", response=response, body=None)
    translated = _translate(error)
    assert translated.retryable is retryable
    if status == 401:
        assert "ANTHROPIC_API_KEY" in str(translated)


def test_connection_errors_are_retryable() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com")
    assert _translate(anthropic.APIConnectionError(request=request)).retryable is True
    assert _translate(anthropic.APITimeoutError(request=request)).retryable is True


async def test_missing_credentials_become_a_model_error(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", "/nonexistent")  # no login profile either
    provider = AnthropicProvider("claude-haiku-4-5")
    request = ModelRequest(messages=[Message.from_text("user", "hi")])
    with pytest.raises(ModelError, match="export ANTHROPIC_API_KEY") as caught:
        await provider.generate(request)
    assert caught.value.retryable is False
