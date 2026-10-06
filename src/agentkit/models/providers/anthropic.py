"""Anthropic provider, on the official SDK. Needs the `agentkit[anthropic]` extra."""

from collections.abc import AsyncIterator
from typing import Any

import anthropic
from anthropic.types import Message as AnthropicMessage

from agentkit.errors import ModelError
from agentkit.models.base import (
    Capabilities,
    ContentPart,
    ImagePart,
    Message,
    ModelRequest,
    ModelResponse,
    OpaquePart,
    StopReason,
    StreamEvent,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    Usage,
)

DEFAULT_CAPABILITIES = Capabilities(
    prompt_caching=True, context_window=200_000, max_output_tokens=64_000
)
# Used only when neither the request nor the model catalogue sets a limit.
DEFAULT_MAX_TOKENS = 16_000

STOP_REASONS: dict[str, StopReason] = {
    "end_turn": "end",
    "pause_turn": "end",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "model_context_window_exceeded": "max_tokens",
    "stop_sequence": "stop_sequence",
    "refusal": "refusal",
}
TOOL_CHOICES = {"required": {"type": "any"}, "none": {"type": "none"}}
# The SDK raises TypeError, not one of its own errors, when it finds no credentials.
SDK_ERRORS = (anthropic.AnthropicError, TypeError)


def to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert non-system messages. Tool results travel in a user turn."""
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            continue
        blocks: list[dict[str, Any]] = []
        for part in message.content:
            if isinstance(part, TextPart):
                blocks.append({"type": "text", "text": part.text})
            elif isinstance(part, ImagePart):
                source: dict[str, Any]
                if part.url:
                    source = {"type": "url", "url": part.url}
                else:
                    source = {"type": "base64", "media_type": part.media_type, "data": part.data}
                blocks.append({"type": "image", "source": source})
            elif isinstance(part, ToolCallPart):
                blocks.append(
                    {"type": "tool_use", "id": part.id, "name": part.name, "input": part.arguments}
                )
            elif isinstance(part, ToolResultPart):
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": part.tool_call_id,
                        "content": part.content,
                        "is_error": part.is_error,
                    }
                )
            elif part.provider == "anthropic":
                blocks.append(part.data)
        role = "assistant" if message.role == "assistant" else "user"
        out.append({"role": role, "content": blocks})
    return out


def from_anthropic_message(message: AnthropicMessage) -> ModelResponse:
    content: list[ContentPart] = []
    for block in message.content:
        if block.type == "text":
            content.append(TextPart(text=block.text))
        elif block.type == "tool_use":
            content.append(ToolCallPart(id=block.id, name=block.name, arguments=dict(block.input)))
        else:
            # Thinking blocks and the like must go back unchanged on the next turn.
            content.append(OpaquePart(provider="anthropic", data=block.to_dict()))
    usage = message.usage
    cache_read = usage.cache_read_input_tokens or 0
    cache_write = usage.cache_creation_input_tokens or 0
    return ModelResponse(
        message=Message(role="assistant", content=content),
        stop_reason=STOP_REASONS.get(message.stop_reason or "end_turn", "end"),
        usage=Usage(
            input_tokens=usage.input_tokens + cache_read + cache_write,
            output_tokens=usage.output_tokens,
            cached_input_tokens=cache_read,
        ),
        model_id=message.model,
        raw=message,
    )


def _translate(exc: Exception) -> ModelError:
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return ModelError(
            f"Anthropic rejected the credentials ({exc.message}). Set ANTHROPIC_API_KEY to a "
            "valid key, or run `ant auth login`."
        )
    if isinstance(exc, anthropic.APIStatusError):
        retryable = exc.status_code in (408, 409, 429) or exc.status_code >= 500
        hint = "It will be retried." if retryable else "Fix the request; retrying will not help."
        return ModelError(
            f"Anthropic answered HTTP {exc.status_code}: {exc.message}. {hint}",
            retryable=retryable,
        )
    if isinstance(exc, anthropic.APIConnectionError):
        return ModelError(
            f"Could not reach the Anthropic API ({exc}). Check your network.", retryable=True
        )
    return ModelError(
        f"Anthropic client error: {exc}. If no credentials are set, export ANTHROPIC_API_KEY "
        "or run `ant auth login`."
    )


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        model_id: str,
        *,
        capabilities: Capabilities = DEFAULT_CAPABILITIES,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self.model_id = model_id
        self.capabilities = capabilities
        self._client_instance = client

    @property
    def _client(self) -> anthropic.AsyncAnthropic:
        if self._client_instance is None:
            try:
                # The kit owns retries, so the SDK's own are switched off.
                self._client_instance = anthropic.AsyncAnthropic(max_retries=0)
            except SDK_ERRORS as exc:
                raise _translate(exc) from exc
        return self._client_instance

    def _shared_kwargs(self, req: ModelRequest) -> dict[str, Any]:
        """Arguments that both message creation and token counting accept."""
        kwargs: dict[str, Any] = {
            "model": self.model_id,
            "messages": to_anthropic_messages(req.messages),
        }
        system = "\n\n".join(m.text for m in req.messages if m.role == "system")
        if system:
            kwargs["system"] = system
        if req.tools:
            kwargs["tools"] = [tool.model_dump() for tool in req.tools]
            if req.tool_choice != "auto":
                kwargs["tool_choice"] = TOOL_CHOICES.get(
                    req.tool_choice, {"type": "tool", "name": req.tool_choice}
                )
        if req.response_schema is not None:
            kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": req.response_schema}
            }
        return kwargs

    def _kwargs(self, req: ModelRequest) -> dict[str, Any]:
        kwargs = self._shared_kwargs(req)
        kwargs["max_tokens"] = req.max_output_tokens or DEFAULT_MAX_TOKENS
        if req.temperature is not None:
            kwargs["temperature"] = req.temperature
        if req.stop:
            kwargs["stop_sequences"] = req.stop
        for key, value in req.provider_options.items():
            current = kwargs.get(key)
            if isinstance(current, dict) and isinstance(value, dict):
                kwargs[key] = {**current, **value}
            else:
                kwargs[key] = value
        return kwargs

    async def generate(self, req: ModelRequest) -> ModelResponse:
        # Streamed under the hood so a large max_tokens cannot hit the SDK's HTTP timeout.
        try:
            async with self._client.messages.stream(**self._kwargs(req)) as stream:
                message = await stream.get_final_message()
        except SDK_ERRORS as exc:
            raise _translate(exc) from exc
        return from_anthropic_message(message)

    async def stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        try:
            async with self._client.messages.stream(**self._kwargs(req)) as stream:
                async for text in stream.text_stream:
                    yield StreamEvent(type="text_delta", text=text)
                message = await stream.get_final_message()
        except SDK_ERRORS as exc:
            raise _translate(exc) from exc
        yield StreamEvent(type="done", response=from_anthropic_message(message))

    async def ping(self) -> str:
        """Count the tokens of a one-word message. Free, and it fails for bad credentials
        or an unknown model ID, which is what a connectivity check should find."""
        probe = ModelRequest(messages=[Message.from_text("user", "ping")])
        await self.count_tokens(probe)
        return f"Anthropic API answers and accepts '{self.model_id}'"

    async def count_tokens(self, req: ModelRequest) -> int:
        try:
            result = await self._client.messages.count_tokens(**self._shared_kwargs(req))
        except SDK_ERRORS as exc:
            raise _translate(exc) from exc
        return result.input_tokens
