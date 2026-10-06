"""Scripted provider for tests and evals. No network."""

import inspect
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

from agentkit.errors import ModelError
from agentkit.models.base import (
    Capabilities,
    Message,
    ModelRequest,
    ModelResponse,
    StreamEvent,
    Usage,
    estimate_tokens,
)

Reply = str | ModelResponse
Scripted = Reply | Exception | Callable[[ModelRequest], Reply | Awaitable[Reply]]

DEFAULT_CAPABILITIES = Capabilities(context_window=1_000_000, max_output_tokens=100_000)


class FakeProvider:
    """Answers each call with the next item of `script`.

    An item is a text reply, a full ModelResponse, an exception to raise, or a
    function of the request. With no script, it echoes the last message.
    Every request is kept in `requests`.
    """

    name = "fake"

    def __init__(
        self,
        script: Sequence[Scripted] | None = None,
        *,
        model_id: str = "fake",
        capabilities: Capabilities = DEFAULT_CAPABILITIES,
    ) -> None:
        self.model_id = model_id
        self.capabilities = capabilities
        self.requests: list[ModelRequest] = []
        self._script = None if script is None else deque(script)

    async def generate(self, req: ModelRequest) -> ModelResponse:
        self.requests.append(req)
        item: Scripted
        if self._script is None:
            item = f"fake: {req.messages[-1].text}" if req.messages else "fake:"
        elif not self._script:
            raise ModelError(
                f"The fake provider's script ran out after {len(self.requests) - 1} calls. "
                "Add more scripted responses."
            )
        else:
            item = self._script.popleft()

        if isinstance(item, Exception):
            raise item
        if callable(item):
            result = item(req)
            item = await result if inspect.isawaitable(result) else result
        if isinstance(item, ModelResponse):
            return item
        return ModelResponse(
            message=Message.from_text("assistant", item),
            stop_reason="end",
            usage=Usage(input_tokens=estimate_tokens(req), output_tokens=max(1, len(item) // 4)),
            model_id=self.model_id,
        )

    async def stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        response = await self.generate(req)
        if response.message.text:
            yield StreamEvent(type="text_delta", text=response.message.text)
        yield StreamEvent(type="done", response=response)

    async def ping(self) -> str:
        return "scripted model; nothing to connect to"

    async def count_tokens(self, req: ModelRequest) -> int:
        return estimate_tokens(req)
