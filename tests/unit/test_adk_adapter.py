import shutil
from pathlib import Path
from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from pydantic import BaseModel
from typer.testing import CliRunner

from agentkit.cli.main import app
from agentkit.config import Config
from agentkit.errors import AgentKitError
from agentkit.guardrails.approvals import ApprovalDecision, ApprovalRequest, CallbackApproval
from agentkit.models.model import Link, Model
from agentkit.models.providers.fake import FakeProvider, Scripted
from agentkit.observability.tracer import read_trace
from agentkit.runtime.adapters import get_adapter
from agentkit.runtime.adapters.adk import AdkAdapter, KitLlm
from agentkit.runtime.context import RunContext
from agentkit.runtime.spec import AgentSpec
from agentkit.tools import ToolRegistry, tool
from agentkit.tools.builtin import BUILTIN_TOOLS
from tests.unit.helpers import RETRY, config, tool_calls

REPO = Path(__file__).parents[2]
NOTES: list[str] = []


@tool(side_effects="none", idempotent=True)
def add(a: int, b: int) -> int:
    """Add two integers.

    Args:
        a: First number.
        b: Second number.
    """
    return a + b


@tool
def save_note(text: str) -> str:
    """Save a note.

    Args:
        text: The note.
    """
    NOTES.append(text)
    return "saved"


SPEC = AgentSpec(
    name="helper",
    description="Adds and notes.",
    instructions="Be exact.",
    tools=["add", "save_note"],
)


def adk(cfg: Config, script: list[Scripted], *extra: Any) -> tuple[AdkAdapter, FakeProvider]:
    """An ADK adapter whose model replays `script` through the kit's model layer."""
    provider = FakeProvider(script)
    model = Model([Link("fake/scripted", provider, timeout_seconds=5)], RETRY)
    registry = ToolRegistry()
    for item in (add, save_note, *extra):
        registry.register(item)
    return AdkAdapter(registry, cfg, model_factory=lambda *a, **k: model), provider


@pytest.fixture(autouse=True)
def clear_notes() -> None:
    NOTES.clear()


async def test_same_spec_runs_end_to_end_on_adk(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    adapter, model = adk(
        cfg, [tool_calls(("add", {"a": 2, "b": 3}), text="Adding."), "The sum is 5."]
    )
    result = await adapter.run(SPEC, "What is 2 + 3?", RunContext(cfg))

    assert (result.stop_reason, result.output) == ("completed", "The sum is 5.")
    assert (result.steps, result.tool_calls) == (2, 1)
    assert [m.role for m in result.messages] == ["system", "user", "assistant", "tool", "assistant"]
    assert result.messages[3].content[0].content == "5"  # type: ignore[union-attr]
    first, second = model.requests
    assert first.messages[0].text.startswith("Be exact.")
    assert [t.name for t in first.tools] == ["add", "save_note"]
    assert first.tools[0].input_schema["required"] == ["a", "b"]
    assert [m.role for m in second.messages][-2:] == ["assistant", "tool"]

    spans = read_trace(Path(result.trace_path or ""))
    assert [s["kind"] for s in spans] == ["run", "model_call", "tool_call", "model_call"]
    assert spans[0]["attributes"]["adapter"] == "adk"
    assert spans[1]["attributes"]["stop_reason"] == "tool_use"


async def test_stream_yields_the_same_event_kinds_as_native(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    adapter, _ = adk(cfg, [tool_calls(("add", {"a": 1, "b": 1}), text="Adding."), "2"])
    events = [e async for e in adapter.stream(SPEC, "1+1?", RunContext(cfg))]
    assert [e.type for e in events] == ["text", "tool_call", "tool_result", "text", "done"]
    assert events[2].tool_result is not None and events[2].tool_result.output == 2


async def test_tool_calls_go_through_the_kit_approval_gate(tmp_path: Path) -> None:
    seen: list[ApprovalRequest] = []
    answers = [
        ApprovalDecision(outcome="deny", note="Not now."),
        ApprovalDecision(outcome="approve"),
    ]

    async def decide(request: ApprovalRequest) -> ApprovalDecision:
        seen.append(request)
        return answers.pop(0)

    cfg = config(tmp_path, guardrails={"max_identical_tool_calls": 5})
    save = tool_calls(("save_note", {"text": "milk"}), text="Saving the note.")
    adapter, model = adk(cfg, [save, save.model_copy(deep=True), "Saved on the second try."])
    ctx = RunContext(cfg, approvals=CallbackApproval(decide))
    result = await adapter.run(SPEC, "Save: milk", ctx)

    assert result.stop_reason == "completed" and NOTES == ["milk"]
    assert seen[0].reason == "Saving the note."
    denied = model.requests[1].messages[-1].content[0]
    assert denied.is_error and "did not approve" in denied.content  # type: ignore[union-attr]


async def test_budget_and_repeat_limits_apply_on_adk(tmp_path: Path) -> None:
    cfg = config(tmp_path, limits={"max_steps": 2})
    script = [tool_calls(("add", {"a": i, "b": 1}), text=f"step {i}") for i in range(5)]
    adapter, model = adk(cfg, script)
    result = await adapter.run(SPEC, "count", RunContext(cfg))
    assert result.stop_reason == "budget_exceeded"
    assert result.error is not None and result.error.kind == "max_steps"
    assert (result.steps, len(model.requests), result.output) == (2, 2, "step 1")

    cfg = config(tmp_path)
    adapter, _ = adk(cfg, [tool_calls(("add", {"a": 1, "b": 1}))] * 10)
    stalled = await adapter.run(SPEC, "loop", RunContext(cfg))
    assert stalled.stop_reason == "stalled" and stalled.tool_calls == 3

    adapter, _ = adk(cfg, [""])
    empty = await adapter.run(SPEC, "say nothing", RunContext(cfg))
    assert empty.stop_reason == "stalled"
    assert empty.error is not None and empty.error.kind == "no_progress"


async def test_output_schema_and_finish_on_adk(tmp_path: Path) -> None:
    class Answer(BaseModel):
        total: int

    cfg = config(tmp_path)
    spec = SPEC.model_copy(update={"tools": [], "output_schema": Answer})
    adapter, model = adk(cfg, ['{"total": 5}'])
    result = await adapter.run(spec, "2+3?", RunContext(cfg))
    assert result.output == Answer(total=5)
    assert model.requests[0].response_schema == Answer.model_json_schema()

    adapter, _ = adk(cfg, ["five"])
    bad = await adapter.run(spec, "2+3?", RunContext(cfg))
    assert bad.error is not None and bad.error.kind == "invalid_output"

    finishing = SPEC.model_copy(update={"tools": ["finish"]})
    done = tool_calls(("finish", {"summary": "Counted.", "result": {"total": 3}}))
    adapter, model = adk(cfg, [done, "never reached"], BUILTIN_TOOLS["finish"])
    finished = await adapter.run(finishing, "count", RunContext(cfg))
    assert finished.output == {"total": 3} and len(model.requests) == 1


async def test_documented_gaps_raise_or_report_clearly(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    adapter, _ = adk(cfg, ["unused"])
    with pytest.raises(AgentKitError, match="takes the input as a string"):
        await adapter.run(SPEC, [], RunContext(cfg))
    with pytest.raises(AgentKitError, match="does not map"):
        await adapter.run(SPEC.model_copy(update={"sub_agents": ["x"]}), "hi", RunContext(cfg))

    async def undecided(request: ApprovalRequest) -> None:
        return None

    adapter, _ = adk(cfg, [tool_calls(("save_note", {"text": "x"}))])
    ctx = RunContext(cfg, approvals=CallbackApproval(undecided))
    waiting = await adapter.run(SPEC, "save", ctx)
    assert waiting.stop_reason == "awaiting_approval" and waiting.pending is None

    adapter, _ = adk(cfg, [tool_calls(("teleport", {}))])
    unknown = await adapter.run(SPEC, "go", RunContext(cfg))
    assert unknown.stop_reason == "error"


def test_to_native_returns_the_adk_agent_and_maps_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("MODEL_PROVIDER", "LOCAL_API_BASE", "LOCAL_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    registry = ToolRegistry()
    registry.register(add)
    spec = SPEC.model_copy(update={"tools": ["add"]})

    hosted = AdkAdapter(registry, config(tmp_path)).to_native(spec)
    assert isinstance(hosted, LlmAgent) and hosted.name == "helper"
    assert [t.name for t in hosted.tools] == ["add"]  # type: ignore[union-attr]
    assert isinstance(hosted.model, LiteLlm) and hosted.model.model == "anthropic/claude-sonnet-5-5"

    # a local model becomes LiteLLM's openai/<id> form with the server's base URL
    monkeypatch.setenv("MODEL_PROVIDER", "local")
    monkeypatch.setenv("LOCAL_MODEL_ID", "qwen/qwen3.5-9b")
    local = AdkAdapter(registry, config(tmp_path)).to_native(spec)
    assert isinstance(local.model, LiteLlm) and local.model.model == "openai/qwen/qwen3.5-9b"
    assert local.model._additional_args["api_base"] == "http://127.0.0.1:1234/v1"
    monkeypatch.delenv("MODEL_PROVIDER")

    catalogue = {"google/g": {"provider": "google", "model_id": "gemini-x"}}
    google_cfg = config(tmp_path, models={"models": catalogue, "roles": {"default": "google/g"}})
    assert AdkAdapter(registry, google_cfg).to_native(spec).model == "gemini-x"

    fake_cfg = config(tmp_path, models={"roles": {"default": "fake/default"}})
    assert isinstance(AdkAdapter(registry, fake_cfg).to_native(spec).model, KitLlm)

    selected = get_adapter(config(tmp_path, runtime={"adapter": "adk"}), registry)
    assert isinstance(selected, AdkAdapter)


def test_the_example_suite_passes_on_the_adk_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("MODEL_PROVIDER", "ANTHROPIC_API_KEY", "AGENTKIT_ENV"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTKIT_RUNTIME__ADAPTER", "adk")
    root = tmp_path / "single_agent"
    shutil.copytree(
        REPO / "examples" / "single_agent", root, ignore=shutil.ignore_patterns(".agentkit")
    )
    result = CliRunner().invoke(app, ["eval", "run", "--project-dir", str(root)])
    assert result.exit_code == 0, result.output
    assert "pass rate: 100.0% (3/3 attempts, 3 cases)" in result.output
