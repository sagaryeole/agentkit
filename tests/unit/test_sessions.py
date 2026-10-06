from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agentkit.cli.main import app
from agentkit.config.schema import ContextSettings
from agentkit.errors import AgentKitError
from agentkit.guardrails.checks import CheckResult, Checks
from agentkit.memory import make_stores
from agentkit.memory.context import REMOVED, SUMMARY_PREFIX, ContextManager, render_transcript
from agentkit.memory.sessions import (
    InMemorySessionStore,
    SessionStore,
    SqliteSessionStore,
    storable,
)
from agentkit.models import Message, OpaquePart, TextPart, ToolCallPart, ToolResultPart
from agentkit.models.base import Capabilities
from agentkit.models.model import Link, Model
from agentkit.models.providers.fake import FakeProvider
from agentkit.observability.tracer import read_trace
from agentkit.runtime.adapters.native import NativeAdapter
from agentkit.runtime.context import RunContext
from agentkit.runtime.session import run_in_session
from agentkit.runtime.spec import AgentSpec
from agentkit.tools import ToolRegistry, tool
from tests.unit.helpers import RETRY, config, scripted_adapter, tool_calls

THINKING = OpaquePart(provider="anthropic", data={"type": "thinking", "signature": "s"})


def user(text: str) -> Message:
    return Message.from_text("user", text)


def assistant(text: str) -> Message:
    return Message.from_text("assistant", text)


def tool_round(call_id: str, result: str) -> list[Message]:
    call = ToolCallPart(id=call_id, name="search", arguments={"q": call_id})
    return [
        Message(role="assistant", content=[THINKING, call]),
        Message(role="tool", content=[ToolResultPart(tool_call_id=call_id, content=result)]),
    ]


# session stores


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_session_stores_keep_history_per_session(tmp_path: Path, kind: str) -> None:
    def make() -> SessionStore:
        if kind == "memory":
            return InMemorySessionStore()
        return SqliteSessionStore(tmp_path / "nested" / "memory.db")

    store = make()
    store.append("a", [user("one"), *tool_round("c1", "found"), assistant("two")])
    store.append("a", [user("three")])
    store.append("b", [user("other")])
    history = store.load("a")
    assert [m.role for m in history] == ["user", "assistant", "tool", "assistant", "user"]
    assert history[1].tool_calls[0].arguments == {"q": "c1"}  # parts survive the round trip
    assert store.load("nobody") == []

    store.truncate("a", keep_last=2)
    assert [m.text for m in store.load("a")] == ["two", "three"]
    store.truncate("a")
    assert store.load("a") == [] and len(store.load("b")) == 1
    if kind == "sqlite":
        assert len(make().load("b")) == 1  # survives a new connection


def test_only_replayable_content_is_stored() -> None:
    thinking_only = Message(role="assistant", content=[THINKING])
    kept = storable(
        [Message.from_text("system", "prompt"), user("hi"), thinking_only, *tool_round("c1", "r")]
    )
    assert [m.role for m in kept] == ["user", "assistant", "tool"]
    assert all(not isinstance(p, OpaquePart) for m in kept for p in m.content)


def test_stores_come_from_config(tmp_path: Path) -> None:
    sessions, state = make_stores(config(tmp_path))
    assert isinstance(sessions, SqliteSessionStore)
    assert (tmp_path / ".agentkit" / "memory.db").is_file()  # anchored to the project
    state.set("session", "s", "k", "v")
    in_memory, _ = make_stores(config(tmp_path, memory={"store": "memory"}))
    assert isinstance(in_memory, InMemorySessionStore)


# context trimming


def settings(strategy: str, keep: int = 2) -> ContextSettings:
    return ContextSettings(strategy=strategy, trigger_ratio=1.0, keep_recent_messages=keep)  # type: ignore[arg-type]


def long_history() -> list[Message]:
    return [
        Message.from_text("system", "Be brief."),
        user("first question"),
        *tool_round("c1", "A" * 2000),
        assistant("first answer"),
        user("second question"),
        *tool_round("c2", "B" * 2000),
        assistant("second answer"),
        user("third question"),
    ]


async def test_a_history_that_fits_is_sent_untouched() -> None:
    history = long_history()
    for strategy in ("drop_tool_results", "summarize"):
        manager = ContextManager(settings(strategy), context_window=100_000)
        assert await manager.view(history, []) is history and not manager.trimmed
    off = ContextManager(settings("off"), context_window=10)
    assert await off.view(history, []) is history


async def test_oldest_tool_results_are_dropped_first() -> None:
    history = long_history()
    manager = ContextManager(settings("drop_tool_results"), context_window=900)
    view = await manager.view(history, [])

    assert manager.trimmed and len(view) == len(history)
    assert view[3].content[0].content == REMOVED  # type: ignore[union-attr]
    assert view[7].content[0].content == "B" * 2000  # type: ignore[union-attr]  # newer one kept
    assert view[2].tool_calls[0].id == "c1"  # the call keeps its (blanked) result
    # reasoning blocks are not replayed once the history has been edited
    assert all(not isinstance(p, OpaquePart) for m in view for p in m.content)
    # the transcript itself is unchanged
    assert history[3].content[0].content == "A" * 2000  # type: ignore[union-attr]
    assert isinstance(history[2].content[0], OpaquePart)

    tight = ContextManager(settings("drop_tool_results", keep=3), context_window=200)
    tight_view = await tight.view(history, [])
    assert tight_view[3].content[0].content == REMOVED  # type: ignore[union-attr]
    assert tight_view[7].content[0].content == "B" * 2000  # type: ignore[union-attr]  # protected


async def test_older_turns_are_summarised_at_a_turn_boundary() -> None:
    asked: list[str] = []

    async def summarize(text: str) -> str:
        asked.append(text)
        return f"summary {len(asked)}"

    history = long_history()
    manager = ContextManager(settings("summarize", keep=1), context_window=900, summarize=summarize)
    view = await manager.view(history, [])

    assert [m.role for m in view] == ["system", "user", "user"]
    assert view[1].text == SUMMARY_PREFIX + "summary 1"
    assert view[2].text == "third question"  # the cut is at the start of a turn
    assert "user: first question" in asked[0] and "assistant called search" in asked[0]
    assert "tool result: AAAA" in asked[0] and "second answer" in asked[0]

    # with more messages protected, the cut moves back to the previous start of a turn
    wider = ContextManager(settings("summarize", keep=2), context_window=900, summarize=summarize)
    kept = await wider.view(history, [])
    assert [m.text for m in kept if m.role == "user"][1:] == ["second question", "third question"]
    asked.pop()

    # the same history is not summarised again
    await manager.view(history, [])
    assert len(asked) == 1
    # a longer history extends the summary instead of starting over
    longer = [*history, assistant("third answer " + "C" * 4000), user("fourth question")]
    extended = await manager.view(longer, [])
    assert len(asked) == 2 and asked[1].startswith(SUMMARY_PREFIX + "summary 1")
    assert "third question" in asked[1] and "first question" not in asked[1]
    assert [m.text for m in extended[1:]] == [SUMMARY_PREFIX + "summary 2", "fourth question"]


async def test_summarise_falls_back_to_dropping_inside_a_single_turn() -> None:
    async def summarize(text: str) -> str:
        raise AssertionError("there is no turn boundary to cut at")

    one_turn = [
        Message.from_text("system", "s"),
        user("q"),
        *tool_round("c1", "A" * 2000),
        *tool_round("c2", "B" * 2000),
    ]
    manager = ContextManager(settings("summarize"), context_window=600, summarize=summarize)
    view = await manager.view(one_turn, [])
    assert view[3].content[0].content == REMOVED  # type: ignore[union-attr]


def test_transcript_rendering() -> None:
    text = render_transcript([user("hi"), *tool_round("c1", "found it"), assistant("done")])
    assert text.splitlines() == [
        "user: hi",
        'assistant called search with {"q": "c1"}',
        "tool result: found it",
        "assistant: done",
    ]


# in the runtime


@tool(side_effects="none")
def fetch_page(n: int) -> str:
    """Fetch page n."""
    return f"page {n}: " + "x" * 3000


def small_window_adapter(script: list[Any], window: int) -> tuple[NativeAdapter, FakeProvider]:
    caps = Capabilities(context_window=window, max_output_tokens=500)
    provider = FakeProvider(script, capabilities=caps)
    model = Model([Link("local/small", provider, timeout_seconds=5)], RETRY)
    registry = ToolRegistry()
    registry.register(fetch_page)
    return NativeAdapter(registry, model_factory=lambda *a, **k: model), provider


SPEC = AgentSpec(name="reader", description="d", instructions="Read.", tools=["fetch_page"])


async def test_a_small_context_window_trims_what_is_sent_not_what_is_kept(tmp_path: Path) -> None:
    script = [tool_calls(("fetch_page", {"n": i})) for i in range(4)] + ["All read."]
    adapter, model = small_window_adapter(script, window=2500)
    cfg = config(tmp_path, memory={"context": {"keep_recent_messages": 2}})
    result = await adapter.run(SPEC, "Read four pages", RunContext(cfg))

    assert result.output == "All read."
    full = [m.content[0].content for m in result.messages if m.role == "tool"]  # type: ignore[union-attr]
    assert all(len(text) > 3000 for text in full)  # the transcript is whole
    last_sent = [m.content[0].content for m in model.requests[-1].messages if m.role == "tool"]  # type: ignore[union-attr]
    assert last_sent[0] == REMOVED and len(last_sent[-1]) > 3000

    spans = [s for s in read_trace(Path(result.trace_path or "")) if s["kind"] == "model_call"]
    assert spans[0]["attributes"]["context_trimmed"] is False
    assert spans[-1]["attributes"]["context_trimmed"] is True


async def test_summarising_uses_the_fast_model_and_counts_against_the_budget(
    tmp_path: Path,
) -> None:
    history = [user("old question"), assistant("old answer " + "y" * 6000)]
    adapter, model = small_window_adapter(["a short summary", "Final."], window=1000)
    cfg = config(tmp_path, memory={"context": {"strategy": "summarize", "keep_recent_messages": 1}})
    ctx = RunContext(cfg)
    result = await adapter.run(SPEC, [*history, user("new question")], ctx)

    assert result.output == "Final." and result.steps == 1  # the summary is not a step
    summary_request, agent_request = model.requests
    assert "compress the earlier part" in summary_request.messages[0].text
    assert "old answer" in summary_request.messages[1].text
    assert [m.text for m in agent_request.messages] == [
        "Read.",
        SUMMARY_PREFIX + "a short summary",
        "new question",
    ]
    assert result.usage.input_tokens > model.requests[1].model_dump_json().__len__() // 40
    spans = read_trace(Path(result.trace_path or ""))
    assert [s["attributes"].get("purpose") for s in spans if s["kind"] == "model_call"] == [
        "summarize_context",
        None,
    ]


# sessions in the runtime


async def test_turns_in_a_session_see_the_earlier_ones(tmp_path: Path) -> None:
    sessions = InMemorySessionStore()
    cfg = config(tmp_path)
    spec = AgentSpec(name="a", description="d", instructions="Chat.", tools=["fetch_page"])
    first_turn = [tool_calls(("fetch_page", {"n": 1})), "It is about x."]
    adapter, model = scripted_adapter([*first_turn, "You asked about page 1."], fetch_page)

    one = await run_in_session(
        adapter, spec, "Read page 1", RunContext(cfg, session_id="s"), sessions
    )
    assert one.output == "It is about x."
    stored = sessions.load("s")
    assert [m.role for m in stored] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]  # no system prompt

    two = await run_in_session(
        adapter, spec, "What did I ask?", RunContext(cfg, session_id="s"), sessions
    )
    assert two.output == "You asked about page 1."
    sent = model.requests[-1].messages
    assert [m.role for m in sent] == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert sent[1].text == "Read page 1" and sent[-1].text == "What did I ask?"
    assert len(sessions.load("s")) == 6
    assert one.run_id != two.run_id  # each turn is its own run, budget and trace

    other = await run_in_session(
        *scripted_adapter(["Hello."])[:1], spec.model_copy(update={"tools": []}), "Hi",
        RunContext(cfg, session_id="t"), sessions,
    )  # fmt: skip
    assert other.output == "Hello." and len(sessions.load("t")) == 2


async def test_an_unfinished_turn_is_stored_as_a_note(tmp_path: Path) -> None:
    sessions = InMemorySessionStore()
    cfg = config(tmp_path, limits={"max_steps": 1})
    spec = AgentSpec(name="a", description="d", instructions="Chat.", tools=["fetch_page"])
    adapter, _ = scripted_adapter([tool_calls(("fetch_page", {"n": 1})), "never"], fetch_page)
    result = await run_in_session(adapter, spec, "Read", RunContext(cfg, session_id="s"), sessions)
    assert result.stop_reason == "budget_exceeded"
    assert [(m.role, m.text) for m in sessions.load("s")] == [
        ("user", "Read"),
        ("assistant", "[This request was not completed: budget_exceeded.]"),
    ]


async def test_session_input_checks_and_missing_session_id(tmp_path: Path) -> None:
    class Polite:
        async def __call__(self, text: str, ctx: RunContext) -> CheckResult:
            return CheckResult(text=text.replace("darn", "****"), ok="rude" not in text)

    sessions = InMemorySessionStore()
    cfg = config(tmp_path)
    spec = AgentSpec(name="a", description="d", instructions="Chat.")
    checks = Checks(user_input=[Polite()])
    adapter, model = scripted_adapter(["ok"])
    ctx = RunContext(cfg, session_id="s", checks=checks)
    await run_in_session(adapter, spec, "darn it", ctx, sessions)
    assert model.requests[0].messages[-1].text == "**** it"
    assert isinstance(sessions.load("s")[0].content[0], TextPart)

    rejected = await run_in_session(
        adapter, spec, "rude", RunContext(cfg, session_id="s", checks=checks), sessions
    )
    assert rejected.stop_reason == "error" and len(sessions.load("s")) == 2
    with pytest.raises(AgentKitError, match="needs an ID"):
        await run_in_session(adapter, spec, "hi", RunContext(cfg), sessions)


def test_chat_command_keeps_a_conversation_across_invocations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("MODEL_PROVIDER", "AGENTKIT_ENV"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "main.py").write_text(
        "from agentkit.runtime.spec import AgentSpec\n"
        "echo = AgentSpec(name='echo', description='d', instructions='Echo.')\n"
    )
    (tmp_path / "agentkit.yaml").write_text("models: {roles: {default: fake/default}}\n")
    runner = CliRunner()
    args = ["chat", "echo", "--session", "demo", "--project-dir", str(tmp_path)]

    first = runner.invoke(app, args, input="hello\n\nagain\n/exit\n")
    assert first.exit_code == 0, first.output
    assert "fake: hello" in first.output and "fake: again" in first.output
    assert "[completed] 1 steps" in first.output
    assert "agentkit chat echo --session demo" in first.output

    second = runner.invoke(app, args, input="/reset\nfresh\n")  # ends at end of input
    assert "Session demo (4 earlier messages)" in second.output
    assert "The conversation was forgotten." in second.output
    third = runner.invoke(app, args, input="/exit\n")
    assert "(2 earlier messages)" in third.output

    unknown = runner.invoke(app, ["chat", "nope", "--project-dir", str(tmp_path)], input="/exit\n")
    assert unknown.exit_code == 1 and "No agent named 'nope'" in unknown.output
