import asyncio

import pytest

from agentkit.config.schema import Price, Retry
from agentkit.errors import ModelError, UnsupportedCapability
from agentkit.models import ImagePart, Message, ModelRequest, ToolSpec, Usage
from agentkit.models.base import Capabilities
from agentkit.models.model import Link, Model, cost_usd
from agentkit.models.providers.fake import FakeProvider, Scripted

RETRY = Retry(max_attempts=3, base_delay_seconds=1.0, max_delay_seconds=3.0)
REQUEST = ModelRequest(messages=[Message.from_text("user", "hello")])
TOOL = ToolSpec(name="lookup", description="Look something up.", input_schema={"type": "object"})


def transient(text: str = "rate limited") -> ModelError:
    return ModelError(text, retryable=True)


class Harness:
    """A Model over fake providers that records backoff delays instead of sleeping."""

    def __init__(self, *scripts: list[Scripted], timeout: float = 5, **link: object) -> None:
        self.providers = [FakeProvider(s, model_id=f"m{i}") for i, s in enumerate(scripts)]
        self.delays: list[float] = []
        chain = [
            Link(key=f"fake/m{i}", provider=p, timeout_seconds=timeout, **link)  # type: ignore[arg-type]
            for i, p in enumerate(self.providers)
        ]
        self.model = Model(chain, RETRY, sleep=self._sleep)

    async def _sleep(self, seconds: float) -> None:
        self.delays.append(seconds)


async def test_transient_errors_are_retried_with_growing_capped_backoff() -> None:
    h = Harness([transient(), transient(), "ok"])
    response = await h.model.generate(REQUEST)
    assert response.message.text == "ok"
    assert len(h.providers[0].requests) == 3
    first, second = h.delays
    assert 0.5 <= first <= 1.0  # base delay with jitter
    assert 1.0 <= second <= 2.0  # doubled


async def test_backoff_never_exceeds_the_maximum() -> None:
    retry = Retry(max_attempts=6, base_delay_seconds=1.0, max_delay_seconds=3.0)
    h = Harness([transient()] * 5 + ["ok"])
    h.model = Model(h.model.chain, retry, sleep=h._sleep)
    await h.model.generate(REQUEST)
    assert len(h.delays) == 5 and max(h.delays) <= 3.0


async def test_retries_stop_at_max_attempts() -> None:
    h = Harness([transient()] * 4)
    with pytest.raises(ModelError, match="rate limited"):
        await h.model.generate(REQUEST)
    assert len(h.providers[0].requests) == 3


async def test_validation_errors_are_not_retried() -> None:
    h = Harness([ModelError("HTTP 400: bad request"), "never reached"])
    with pytest.raises(ModelError, match="HTTP 400"):
        await h.model.generate(REQUEST)
    assert len(h.providers[0].requests) == 1 and h.delays == []


async def test_a_slow_call_times_out_and_is_retried() -> None:
    async def slow(req: ModelRequest) -> str:
        await asyncio.sleep(1)
        return "too late"

    h = Harness([slow, "quick"], timeout=0.01)
    assert (await h.model.generate(REQUEST)).message.text == "quick"

    h = Harness([slow, slow, slow], timeout=0.01)
    with pytest.raises(ModelError, match="did not answer within 0.01s"):
        await h.model.generate(REQUEST)


async def test_fallback_is_used_after_the_first_model_fails_its_retries() -> None:
    h = Harness([transient()] * 3, ["from the fallback"])
    response = await h.model.generate(REQUEST)
    assert response.message.text == "from the fallback"
    assert response.model_id == "m1"
    assert len(h.providers[0].requests) == 3 and len(h.providers[1].requests) == 1


async def test_the_last_error_is_raised_when_the_whole_chain_fails() -> None:
    h = Harness([ModelError("first down")], [ModelError("second down")])
    with pytest.raises(ModelError, match="second down"):
        await h.model.generate(REQUEST)


def test_cost_uses_the_cached_rate_for_cache_reads() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=500_000, cached_input_tokens=400_000)
    assert cost_usd(usage, Price(input=2, output=10, cached_input=0.2)) == pytest.approx(
        0.6 * 2 + 0.4 * 0.2 + 0.5 * 10
    )
    assert cost_usd(usage, Price(input=2, output=10)) == pytest.approx(2 + 5)


async def test_cost_is_written_into_usage_when_the_model_is_priced() -> None:
    priced = Harness(["four"], price=Price(input=1_000_000, output=2_000_000))
    usage = (await priced.model.generate(REQUEST)).usage
    assert usage.cost_usd == pytest.approx(usage.input_tokens + 2 * usage.output_tokens)

    unpriced = Harness(["four"])
    assert (await unpriced.model.generate(REQUEST)).usage.cost_usd is None


async def test_catalogue_defaults_fill_unset_request_fields_only() -> None:
    h = Harness(["a", "b"], defaults={"max_output_tokens": 100})
    await h.model.generate(REQUEST)
    await h.model.generate(REQUEST.model_copy(update={"max_output_tokens": 7}))
    assert [r.max_output_tokens for r in h.providers[0].requests] == [100, 7]


async def test_unsupported_requests_fail_before_any_call() -> None:
    caps = Capabilities(
        tools=False,
        vision=False,
        forced_tool_choice=False,
        context_window=8192,
        max_output_tokens=100,
    )
    provider = FakeProvider(["unused"], capabilities=caps)
    model = Model([Link(key="local/small", provider=provider, timeout_seconds=5)], RETRY)

    with pytest.raises(UnsupportedCapability, match="'local/small' does not support tool calling"):
        await model.generate(REQUEST.model_copy(update={"tools": [TOOL]}))
    image = Message(role="user", content=[ImagePart(data="aGk=")])
    with pytest.raises(UnsupportedCapability, match="image input"):
        await model.generate(ModelRequest(messages=[image]))
    with pytest.raises(UnsupportedCapability, match="at most 100 output tokens"):
        await model.generate(REQUEST.model_copy(update={"max_output_tokens": 101}))
    assert provider.requests == []

    caps = caps.model_copy(update={"tools": True})
    model = Model([Link("m", FakeProvider(capabilities=caps), timeout_seconds=5)], RETRY)
    with pytest.raises(UnsupportedCapability, match="forcing a tool call"):
        await model.generate(REQUEST.model_copy(update={"tools": [TOOL], "tool_choice": "lookup"}))


SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}


async def test_json_is_prompted_and_validated_when_structured_output_is_missing() -> None:
    caps = Capabilities(structured_output=False, context_window=8192, max_output_tokens=1000)
    provider = FakeProvider(["not json", '{"n": "x"}', '```json\n{"n": 3}\n```'], capabilities=caps)
    model = Model([Link("local/small", provider, timeout_seconds=5)], RETRY)

    response = await model.generate(REQUEST.model_copy(update={"response_schema": SCHEMA}))

    assert response.message.text == '{"n": 3}'
    assert len(provider.requests) == 3
    assert provider.requests[0].response_schema is None
    assert "JSON Schema" in provider.requests[0].messages[0].text
    assert "not valid" in provider.requests[2].messages[-1].text
    assert response.usage.output_tokens == sum(
        max(1, len(t) // 4) for t in ("not json", '{"n": "x"}', '```json\n{"n": 3}\n```')
    )


async def test_json_fallback_gives_up_after_bounded_attempts() -> None:
    caps = Capabilities(structured_output=False, context_window=8192, max_output_tokens=1000)
    provider = FakeProvider(["nope"] * 5, capabilities=caps)
    model = Model([Link("local/small", provider, timeout_seconds=5)], RETRY)
    with pytest.raises(ModelError, match="did not produce JSON matching the schema in 3"):
        await model.generate(REQUEST.model_copy(update={"response_schema": SCHEMA}))
    assert len(provider.requests) == 3


async def test_native_structured_output_is_passed_through() -> None:
    provider = FakeProvider(['{"n": 1}'])
    model = Model([Link("fake/m", provider, timeout_seconds=5)], RETRY)
    await model.generate(REQUEST.model_copy(update={"response_schema": SCHEMA}))
    assert provider.requests[0].response_schema == SCHEMA


async def test_stream_falls_back_before_the_first_event_and_prices_the_result() -> None:
    h = Harness([transient()], ["streamed"], price=Price(input=1, output=1))
    events = [event async for event in h.model.stream(REQUEST)]
    assert [e.type for e in events] == ["text_delta", "done"]
    assert events[0].text == "streamed"
    assert events[1].response is not None and events[1].response.usage.cost_usd is not None


async def test_fake_script_exhaustion_says_what_to_do() -> None:
    provider = FakeProvider(["only one"])
    await provider.generate(REQUEST)
    with pytest.raises(ModelError, match="Add more scripted responses"):
        await provider.generate(REQUEST)
