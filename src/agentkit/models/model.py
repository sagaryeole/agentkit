"""What agent code holds: a provider plus the behaviour every provider shares.

Retry, timeout, fallback, cost calculation and the JSON fallback for models
without structured output are implemented here once, around any provider.
"""

import asyncio
import json
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from agentkit.config.schema import Price, Retry
from agentkit.errors import ModelError
from agentkit.models.base import (
    Capabilities,
    Message,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    StreamEvent,
    Usage,
    check_request,
)

JSON_ATTEMPTS = 3


@dataclass(frozen=True)
class Link:
    """One model in a fallback chain."""

    key: str
    provider: ModelProvider
    timeout_seconds: float
    price: Price | None = None
    defaults: Mapping[str, Any] = field(default_factory=dict)


def cost_usd(usage: Usage, price: Price) -> float:
    cached_rate = price.input if price.cached_input is None else price.cached_input
    fresh = usage.input_tokens - usage.cached_input_tokens
    total = fresh * price.input + usage.cached_input_tokens * cached_rate
    total += usage.output_tokens * price.output
    return total / 1_000_000


async def generate_json(provider: ModelProvider, req: ModelRequest) -> ModelResponse:
    """Structured output for a model without it: ask for JSON, validate, retry a few times."""
    schema = req.response_schema or {}
    instruction = (
        "Reply with a single JSON value that conforms to this JSON Schema. "
        "No prose, no code fences.\n" + json.dumps(schema)
    )
    messages = [Message.from_text("system", instruction), *req.messages]
    usage = Usage(input_tokens=0, output_tokens=0)
    problem = ""
    for _ in range(JSON_ATTEMPTS):
        response = await provider.generate(
            req.model_copy(update={"messages": messages, "response_schema": None})
        )
        usage = usage.plus(response.usage)
        text = response.message.text.strip().removeprefix("```json").strip("`").strip()
        try:
            jsonschema.validate(json.loads(text), schema)
        except (json.JSONDecodeError, jsonschema.ValidationError) as exc:
            problem = exc.msg if isinstance(exc, json.JSONDecodeError) else exc.message
            correction = f"That reply was not valid: {problem}. Reply again with only the JSON."
            messages = [*messages, response.message, Message.from_text("user", correction)]
            continue
        return response.model_copy(
            update={"message": Message.from_text("assistant", text), "usage": usage}
        )
    raise ModelError(
        f"The model did not produce JSON matching the schema in {JSON_ATTEMPTS} attempts "
        f"(last problem: {problem}). Simplify the schema or use a model with structured output."
    )


class Model:
    """A model resolved from a role: the primary provider followed by its fallbacks."""

    def __init__(
        self,
        chain: Sequence[Link],
        retry: Retry,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.chain = list(chain)
        self._retry = retry
        self._sleep = sleep

    @property
    def key(self) -> str:
        return self.chain[0].key

    @property
    def name(self) -> str:
        return self.chain[0].provider.name

    @property
    def capabilities(self) -> Capabilities:
        return self.chain[0].provider.capabilities

    @staticmethod
    def _prepared(link: Link, req: ModelRequest) -> ModelRequest:
        """Fill unset request fields from the catalogue defaults, then check capabilities."""
        unset = {k: v for k, v in link.defaults.items() if getattr(req, k, None) is None}
        req = req.model_copy(update=unset)
        check_request(req, link.provider.capabilities, link.key)
        return req

    @staticmethod
    def _priced(link: Link, response: ModelResponse) -> ModelResponse:
        if link.price is None:
            return response
        usage = response.usage.model_copy(update={"cost_usd": cost_usd(response.usage, link.price)})
        return response.model_copy(update={"usage": usage})

    async def _once(self, link: Link, req: ModelRequest) -> ModelResponse:
        try:
            async with asyncio.timeout(link.timeout_seconds):
                if (
                    req.response_schema is not None
                    and not link.provider.capabilities.structured_output
                ):
                    return await generate_json(link.provider, req)
                return await link.provider.generate(req)
        except TimeoutError as exc:
            raise ModelError(
                f"Model '{link.key}' did not answer within {link.timeout_seconds:g}s. "
                "Raise timeouts.model_call_seconds or use a faster model.",
                retryable=True,
            ) from exc

    async def _with_retry(self, link: Link, req: ModelRequest) -> ModelResponse:
        attempt = 1
        while True:
            try:
                return self._priced(link, await self._once(link, req))
            except ModelError as exc:
                if not exc.retryable or attempt >= self._retry.max_attempts:
                    raise
            backoff = self._retry.base_delay_seconds * 2 ** (attempt - 1)
            delay = min(self._retry.max_delay_seconds, backoff)
            await self._sleep(delay * random.uniform(0.5, 1.0))
            attempt += 1

    async def generate(self, req: ModelRequest) -> ModelResponse:
        """Call the model, retrying transient failures and then moving down the fallback chain."""
        failure: ModelError | None = None
        for link in self.chain:
            prepared = self._prepared(link, req)
            try:
                return await self._with_retry(link, prepared)
            except ModelError as exc:
                failure = exc
        assert failure is not None
        raise failure

    async def stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Stream from the model. Falls back only if a model fails before its first event;
        there is no retry and no per-call timeout once a stream has started."""
        failure: ModelError | None = None
        for link in self.chain:
            prepared = self._prepared(link, req)
            started = False
            try:
                async for event in link.provider.stream(prepared):
                    started = True
                    if event.response is not None:
                        event = event.model_copy(
                            update={"response": self._priced(link, event.response)}
                        )
                    yield event
                return
            except ModelError as exc:
                if started:
                    raise
                failure = exc
        assert failure is not None
        raise failure

    async def count_tokens(self, req: ModelRequest) -> int:
        return await self.chain[0].provider.count_tokens(req)
