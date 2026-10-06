"""Provider for any server that speaks the OpenAI chat-completions protocol."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from agentkit.errors import ModelError
from agentkit.models.base import (
    Capabilities,
    ContentPart,
    ImagePart,
    Message,
    ModelRequest,
    ModelResponse,
    StopReason,
    StreamEvent,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    Usage,
    estimate_tokens,
)

STOP_REASONS: dict[str, StopReason] = {
    "stop": "end",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}


def to_openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "tool":
            out.extend(
                {"role": "tool", "tool_call_id": part.tool_call_id, "content": part.content}
                for part in message.content
                if isinstance(part, ToolResultPart)
            )
            continue
        parts: list[dict[str, Any]] = []
        calls: list[dict[str, Any]] = []
        for part in message.content:
            if isinstance(part, TextPart):
                parts.append({"type": "text", "text": part.text})
            elif isinstance(part, ImagePart):
                url = part.url or f"data:{part.media_type};base64,{part.data}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
            elif isinstance(part, ToolCallPart):
                function = {"name": part.name, "arguments": json.dumps(part.arguments)}
                calls.append({"id": part.id, "type": "function", "function": function})
        entry: dict[str, Any] = {"role": message.role}
        if all(p["type"] == "text" for p in parts):
            entry["content"] = "".join(p["text"] for p in parts)
        else:
            entry["content"] = parts
        if calls:
            entry["tool_calls"] = calls
        out.append(entry)
    return out


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(
        self,
        model_id: str,
        *,
        api_base: str,
        api_key: str,
        capabilities: Capabilities,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model_id = model_id
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.capabilities = capabilities
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        # The kit's own wrapper enforces the call timeout; only connecting is bounded here.
        return httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=httpx.Timeout(None, connect=5.0),
            transport=self._transport,
        )

    def _payload(self, req: ModelRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": to_openai_messages(req.messages),
        }
        if req.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in req.tools
            ]
            if req.tool_choice in ("required", "none"):
                payload["tool_choice"] = req.tool_choice
            elif req.tool_choice != "auto":
                payload["tool_choice"] = {"type": "function", "function": {"name": req.tool_choice}}
        if req.max_output_tokens is not None:
            payload["max_tokens"] = req.max_output_tokens
        if req.temperature is not None:
            payload["temperature"] = req.temperature
        if req.stop:
            payload["stop"] = req.stop
        if req.response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": req.response_schema, "strict": True},
            }
        payload.update(req.provider_options)
        return payload

    def _connection_error(self, exc: Exception) -> ModelError:
        return ModelError(
            f"Could not reach {self.api_base} ({exc}). Check the base URL and your network.",
            retryable=True,
        )

    def _status_error(self, status: int, body: str) -> ModelError:
        retryable = status in (408, 409, 429) or status >= 500
        return ModelError(
            f"{self.api_base} answered HTTP {status} for model '{self.model_id}': {body[:500]}",
            retryable=retryable,
        )

    def _response(self, data: dict[str, Any]) -> ModelResponse:
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError(
                f"{self.api_base} returned a response with no choices: {str(data)[:300]}. "
                "Check that the base URL points at a chat-completions API.",
            ) from exc
        content: list[ContentPart] = []
        if message.get("content"):
            content.append(TextPart(text=message["content"]))
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError as exc:
                raise ModelError(
                    f"Model '{self.model_id}' wrote tool arguments that are not valid JSON. "
                    "Retry, or use a model with more reliable tool calling.",
                    retryable=True,
                ) from exc
            content.append(
                ToolCallPart(
                    id=call.get("id") or f"call_{index}",
                    name=function.get("name") or "",
                    arguments=arguments,
                )
            )
        usage = data.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        return ModelResponse(
            message=Message(role="assistant", content=content),
            stop_reason=STOP_REASONS.get(choice.get("finish_reason") or "stop", "end"),
            usage=Usage(
                input_tokens=usage.get("prompt_tokens") or 0,
                output_tokens=usage.get("completion_tokens") or 0,
                cached_input_tokens=details.get("cached_tokens") or 0,
            ),
            model_id=self.model_id,
            raw=data,
        )

    async def generate(self, req: ModelRequest) -> ModelResponse:
        async with self._client() as client:
            try:
                resp = await client.post(
                    f"{self.api_base}/chat/completions", json=self._payload(req)
                )
            except httpx.TransportError as exc:
                raise self._connection_error(exc) from exc
        if resp.status_code != 200:
            raise self._status_error(resp.status_code, resp.text)
        return self._response(resp.json())

    async def stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        payload = self._payload(req)
        payload["stream"] = True
        payload.setdefault("stream_options", {"include_usage": True})
        text: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage: dict[str, Any] | None = None
        url = f"{self.api_base}/chat/completions"
        try:
            async with self._client() as client, client.stream("POST", url, json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode(errors="replace")
                    raise self._status_error(resp.status_code, body)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:") or line.endswith("[DONE]"):
                        continue
                    chunk = json.loads(line[len("data:") :])
                    usage = chunk.get("usage") or usage
                    for choice in chunk.get("choices") or []:
                        finish_reason = choice.get("finish_reason") or finish_reason
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            text.append(delta["content"])
                            yield StreamEvent(type="text_delta", text=delta["content"])
                        for call in delta.get("tool_calls") or []:
                            slot = calls.setdefault(
                                call.get("index", 0), {"id": None, "name": "", "arguments": ""}
                            )
                            function = call.get("function") or {}
                            slot["id"] = call.get("id") or slot["id"]
                            slot["name"] += function.get("name") or ""
                            slot["arguments"] += function.get("arguments") or ""
        except httpx.TransportError as exc:
            raise self._connection_error(exc) from exc

        message: dict[str, Any] = {
            "content": "".join(text),
            "tool_calls": [
                {"id": c["id"], "function": {"name": c["name"], "arguments": c["arguments"]}}
                for _, c in sorted(calls.items())
            ],
        }
        completion = {
            "choices": [{"message": message, "finish_reason": finish_reason}],
            "usage": usage,
        }
        yield StreamEvent(type="done", response=self._response(completion))

    async def count_tokens(self, req: ModelRequest) -> int:
        # The protocol has no counting endpoint, so this is an estimate.
        return estimate_tokens(req)
