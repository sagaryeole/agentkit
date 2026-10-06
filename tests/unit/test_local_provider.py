import json

import httpx
import pytest

from agentkit.errors import ModelError
from agentkit.models import ImagePart, Message, ModelRequest, TextPart, ToolCallPart, ToolResultPart
from agentkit.models.base import ToolSpec
from agentkit.models.providers.local import LocalProvider, list_local_models
from agentkit.models.providers.openai_compat import to_openai_messages

BASE = "http://127.0.0.1:1234/v1"
REQUEST = ModelRequest(messages=[Message.from_text("user", "hi")])
COMPLETION = {
    "choices": [
        {
            "finish_reason": "tool_calls",
            "message": {
                "content": "Let me check.",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "multiply", "arguments": '{"a": 2, "b": 3}'}}
                ],
            },
        }
    ],
    "usage": {"prompt_tokens": 12, "completion_tokens": 5},
}


class Server:
    """An in-memory stand-in for a local model server."""

    def __init__(self, models: list[str], completion: dict[str, object] | None = None) -> None:
        self.models = models
        self.completion = completion or COMPLETION
        self.bodies: list[dict[str, object]] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": m} for m in self.models]})
        if request.url.path == "/v1/chat/completions":
            self.bodies.append(json.loads(request.content))
            return httpx.Response(200, json=self.completion)
        return httpx.Response(404, json={"error": "Unexpected endpoint"})


def refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("All connection attempts failed")


async def test_generate_parses_text_tool_calls_and_usage() -> None:
    server = Server(["qwen"])
    provider = LocalProvider("qwen", api_base=BASE, server="lmstudio", transport=server.transport)
    tool = ToolSpec(name="multiply", description="Multiply.", input_schema={"type": "object"})

    response = await provider.generate(REQUEST.model_copy(update={"tools": [tool]}))

    assert response.stop_reason == "tool_use"
    assert response.message.text == "Let me check."
    assert response.message.tool_calls == [
        ToolCallPart(id="c1", name="multiply", arguments={"a": 2, "b": 3})
    ]
    assert (response.usage.input_tokens, response.usage.output_tokens) == (12, 5)
    body = server.bodies[0]
    assert body["model"] == "qwen"
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "multiply",
                "description": "Multiply.",
                "parameters": {"type": "object"},
            },
        }
    ]


async def test_stopped_server_names_the_cause() -> None:
    transport = httpx.MockTransport(refuse)
    lmstudio = LocalProvider("qwen", api_base=BASE, server="lmstudio", transport=transport)
    with pytest.raises(ModelError, match="No lmstudio model server is answering at .*1234/v1"):
        await lmstudio.generate(REQUEST)

    ollama = LocalProvider(
        "qwen", api_base="http://127.0.0.1:11434/v1", server="ollama", transport=transport
    )
    with pytest.raises(ModelError, match="Start it with `ollama serve`"):
        await ollama.generate(REQUEST)


async def test_base_url_missing_v1_or_port_names_the_cause() -> None:
    # Without "/v1" the server answers, but not with a model list; without the port nothing answers.
    cases = [
        ("http://127.0.0.1:1234", Server(["qwen"]).transport),
        ("http://127.0.0.1/v1", httpx.MockTransport(refuse)),
    ]
    for base, transport in cases:
        provider = LocalProvider("qwen", api_base=base, server="lmstudio", transport=transport)
        with pytest.raises(ModelError, match="must include the port and end in '/v1'") as caught:
            await provider.generate(REQUEST)
        assert "http://127.0.0.1:1234/v1" in str(caught.value)


async def test_wrong_model_id_lists_what_the_server_offers() -> None:
    server = Server(["qwen/qwen3.5-9b", "llama3"])
    provider = LocalProvider("gemma", api_base=BASE, server="lmstudio", transport=server.transport)
    with pytest.raises(ModelError) as caught:
        await provider.generate(REQUEST)
    message = str(caught.value)
    assert "Model 'gemma' is not available on the lmstudio server" in message
    assert "Available: qwen/qwen3.5-9b, llama3" in message
    assert server.bodies == []


async def test_unset_model_id_asks_for_one() -> None:
    server = Server(["llama3"])
    provider = LocalProvider("", api_base=BASE, server="ollama", transport=server.transport)
    with pytest.raises(ModelError, match="Set LOCAL_MODEL_ID to one of the IDs .* llama3"):
        await provider.generate(REQUEST)


async def test_discovery_returns_the_server_ids() -> None:
    server = Server(["a", "b"])
    assert await list_local_models(BASE, transport=server.transport) == ["a", "b"]


async def test_stream_yields_deltas_then_the_assembled_response() -> None:
    chunks = [
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "function": {"name": "multiply", "arguments": '{"a":'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": " 2}"}}]},
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 4}},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "qwen"}]})
        return httpx.Response(200, text=body)

    provider = LocalProvider("qwen", api_base=BASE, transport=httpx.MockTransport(handle))
    events = [event async for event in provider.stream(REQUEST)]

    assert [e.text for e in events[:-1]] == ["Hel", "lo"]
    final = events[-1].response
    assert final is not None and final.message.text == "Hello"
    assert final.message.tool_calls == [ToolCallPart(id="c1", name="multiply", arguments={"a": 2})]
    assert final.stop_reason == "tool_use" and final.usage.output_tokens == 4


async def test_http_errors_are_classified() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "qwen"}]})
        return httpx.Response(int(request.headers["x-status"]), text="boom")

    provider = LocalProvider("qwen", api_base=BASE, transport=httpx.MockTransport(handle))
    for status, retryable in ((400, False), (429, True), (503, True)):
        options = {"extra": status}
        provider._client = lambda s=status: httpx.AsyncClient(  # type: ignore[method-assign]
            headers={"x-status": str(s)}, transport=provider._transport
        )
        with pytest.raises(ModelError, match=f"HTTP {status}") as caught:
            await provider.generate(REQUEST.model_copy(update={"provider_options": options}))
        assert caught.value.retryable is retryable


def test_messages_convert_to_the_chat_completions_shape() -> None:
    messages = [
        Message.from_text("system", "Be brief."),
        Message(role="user", content=[TextPart(text="What is this?"), ImagePart(data="aGk=")]),
        Message(
            role="assistant",
            content=[ToolCallPart(id="c1", name="look", arguments={"q": "x"})],
        ),
        Message(role="tool", content=[ToolResultPart(tool_call_id="c1", content="a cat")]),
    ]
    assert to_openai_messages(messages) == [
        {"role": "system", "content": "Be brief."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is this?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGk="}},
            ],
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "look", "arguments": '{"q": "x"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "a cat"},
    ]
