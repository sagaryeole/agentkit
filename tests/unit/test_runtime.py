from pathlib import Path

import pytest
from pydantic import BaseModel

from agentkit.config.schema import Limits
from agentkit.errors import AgentKitError, UnsupportedCapability
from agentkit.guardrails.approvals import (
    ApprovalDecision,
    ApprovalRequest,
    AutoApprove,
    CallbackApproval,
    CliApproval,
    make_channel,
)
from agentkit.guardrails.budget import Budget, BudgetExceeded
from agentkit.guardrails.checks import CheckResult, Checks, Redactor, SchemaCheck
from agentkit.models import Usage
from agentkit.models.base import Capabilities
from agentkit.models.model import Link, Model
from agentkit.models.providers.fake import FakeProvider
from agentkit.runtime.adapters import get_adapter
from agentkit.runtime.adapters.native import NativeAdapter
from agentkit.runtime.context import RunContext
from agentkit.runtime.project import load_project
from agentkit.runtime.spec import AgentSpec, PendingApproval
from agentkit.tools import ToolRegistry, tool
from tests.unit.helpers import RETRY, config, scripted_adapter, tool_calls

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
    """Save a note for later.

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


@pytest.fixture(autouse=True)
def clear_notes() -> None:
    NOTES.clear()


async def test_single_agent_with_a_tool_runs_end_to_end(tmp_path: Path) -> None:
    adapter, model = scripted_adapter(
        [tool_calls(("add", {"a": 2, "b": 3}), text="Adding."), "The sum is 5."], add, save_note
    )
    result = await adapter.run(SPEC, "What is 2 + 3?", RunContext(config(tmp_path)))

    assert (result.stop_reason, result.output) == ("completed", "The sum is 5.")
    assert (result.steps, result.tool_calls) == (2, 1)
    assert result.usage.input_tokens > 0 and result.error is None
    assert [m.role for m in result.messages] == ["system", "user", "assistant", "tool", "assistant"]
    assert result.messages[3].content[0].content == "5"  # type: ignore[union-attr]
    first = model.requests[0]
    assert first.messages[0].text == "Be exact."
    assert [t.name for t in first.tools] == ["add", "save_note"]


async def test_an_agent_only_gets_the_tools_its_spec_lists(tmp_path: Path) -> None:
    adapter, model = scripted_adapter(["hi"], add, save_note)
    await adapter.run(
        SPEC.model_copy(update={"tools": ["add"]}), "hi", RunContext(config(tmp_path))
    )
    assert [t.name for t in model.requests[0].tools] == ["add"]

    with pytest.raises(AgentKitError, match="No tool named 'teleport'"):
        await adapter.run(
            SPEC.model_copy(update={"tools": ["teleport"]}), "hi", RunContext(config(tmp_path))
        )


async def test_calling_an_unknown_tool_is_reported_to_the_model(tmp_path: Path) -> None:
    adapter, model = scripted_adapter([tool_calls(("teleport", {})), "I cannot do that."], add)
    spec = SPEC.model_copy(update={"tools": ["add"]})
    result = await adapter.run(spec, "go", RunContext(config(tmp_path)))
    assert result.stop_reason == "completed"
    assert "no tool named 'teleport'" in model.requests[1].messages[-1].content[0].content  # type: ignore[union-attr]


async def test_stream_yields_text_tool_events_and_done(tmp_path: Path) -> None:
    adapter, _ = scripted_adapter([tool_calls(("add", {"a": 1, "b": 1}), text="Adding."), "2"], add)
    spec = SPEC.model_copy(update={"tools": ["add"]})
    events = [e async for e in adapter.stream(spec, "1+1?", RunContext(config(tmp_path)))]
    assert [e.type for e in events] == ["text", "tool_call", "tool_result", "done"]
    assert events[2].tool_result is not None and events[2].tool_result.output == 2
    assert events[3].result is not None and events[3].result.output == "2"


# budgets


async def test_step_limit_stops_the_run_and_names_the_limit(tmp_path: Path) -> None:
    script = [tool_calls(("add", {"a": i, "b": 1}), text=f"step {i}") for i in range(10)]
    adapter, model = scripted_adapter(script, add)
    spec = SPEC.model_copy(update={"tools": ["add"]})
    result = await adapter.run(spec, "count", RunContext(config(tmp_path, limits={"max_steps": 3})))

    assert result.stop_reason == "budget_exceeded"
    assert result.error is not None and result.error.kind == "max_steps"
    assert "limits.max_steps (3 of 3)" in result.error.message
    assert (result.steps, len(model.requests)) == (3, 3)
    assert result.output == "step 2"  # whatever partial result exists


async def test_tool_call_token_and_cost_limits(tmp_path: Path) -> None:
    spec = SPEC.model_copy(update={"tools": ["add"]})
    three_calls = tool_calls(*[("add", {"a": i, "b": 0}) for i in range(3)])

    adapter, _ = scripted_adapter([three_calls, "done"], add)
    ctx = RunContext(config(tmp_path, limits={"max_tool_calls": 2}))
    result = await adapter.run(spec, "go", ctx)
    assert result.error is not None and result.error.kind == "max_tool_calls"
    assert result.tool_calls == 2

    adapter, _ = scripted_adapter([tool_calls(("add", {"a": 1, "b": 1})), "done"], add)
    ctx = RunContext(config(tmp_path, limits={"max_input_tokens": 10}))
    result = await adapter.run(spec, "go", ctx)
    assert result.error is not None and result.error.kind == "max_input_tokens"

    # limits on the AgentSpec override the global ones
    limits = Limits(**{**config(tmp_path).limits.model_dump(), "max_steps": 1})
    adapter, _ = scripted_adapter([tool_calls(("add", {"a": 1, "b": 1})), "done"], add)
    limited = spec.model_copy(update={"limits": limits})
    result = await adapter.run(limited, "go", RunContext(config(tmp_path)))
    assert result.error is not None and result.error.kind == "max_steps"


def test_budget_tracks_cost_and_wall_time(tmp_path: Path) -> None:
    now = [0.0]
    limits = config(tmp_path, limits={"max_cost_usd": 1.0, "max_wall_seconds": 60}).limits
    budget = Budget(limits, clock=lambda: now[0])
    budget.check()
    budget.record_model(Usage(input_tokens=1, output_tokens=1, cost_usd=0.6))
    budget.check()
    budget.record_model(Usage(input_tokens=1, output_tokens=1, cost_usd=0.6))
    with pytest.raises(BudgetExceeded, match="max_cost_usd") as caught:
        budget.check()
    assert caught.value.limit == "max_cost_usd"

    timed = Budget(limits, clock=lambda: now[0])
    now[0] = 61.0
    with pytest.raises(BudgetExceeded, match="max_wall_seconds"):
        timed.check()

    free = Budget(config(tmp_path, limits={"max_cost_usd": 0}).limits)
    free.record_model(Usage(input_tokens=1, output_tokens=1, cost_usd=0.0))
    free.check()  # a zero-cost run never trips a zero cost limit


# loop and stall detection


async def test_repeating_the_same_tool_call_stalls_the_run(tmp_path: Path) -> None:
    adapter, model = scripted_adapter([tool_calls(("add", {"a": 1, "b": 1}))] * 10, add)
    spec = SPEC.model_copy(update={"tools": ["add"]})
    result = await adapter.run(spec, "go", RunContext(config(tmp_path)))
    assert result.stop_reason == "stalled"
    assert result.error is not None and result.error.kind == "repeated_tool_call"
    assert result.tool_calls == 3 and len(model.requests) == 4


async def test_steps_with_no_tool_call_and_no_answer_stall_the_run(tmp_path: Path) -> None:
    adapter, model = scripted_adapter(["", "  ", "never reached"])
    spec = SPEC.model_copy(update={"tools": []})
    result = await adapter.run(spec, "go", RunContext(config(tmp_path)))
    assert result.stop_reason == "stalled"
    assert result.error is not None and result.error.kind == "no_progress"
    assert len(model.requests) == 2


# approval gate


def deciding(*decisions: ApprovalDecision | None) -> tuple[CallbackApproval, list[ApprovalRequest]]:
    seen: list[ApprovalRequest] = []
    queue = list(decisions)

    async def decide(request: ApprovalRequest) -> ApprovalDecision | None:
        seen.append(request)
        return queue.pop(0)

    return CallbackApproval(decide), seen


SAVE = tool_calls(("save_note", {"text": "buy milk"}), text="I will save that.")


async def test_a_denied_approval_is_visible_to_the_model_and_the_run_continues(
    tmp_path: Path,
) -> None:
    channel, seen = deciding(ApprovalDecision(outcome="deny", note="Not now."))
    adapter, model = scripted_adapter([SAVE, "I could not save the note."], add, save_note)
    result = await adapter.run(
        SPEC, "Save: buy milk", RunContext(config(tmp_path), approvals=channel)
    )

    assert (result.stop_reason, result.output) == ("completed", "I could not save the note.")
    assert NOTES == []
    request = seen[0]
    assert (request.tool_name, request.side_effects) == ("save_note", "write")
    assert request.arguments == {"text": "buy milk"}
    assert request.reason == "I will save that."  # the model's stated reason
    told = model.requests[1].messages[-1].content[0]
    assert told.is_error and "did not approve" in told.content  # type: ignore[union-attr]
    assert "Not now." in told.content  # type: ignore[union-attr]


async def test_approved_and_edited_calls_run(tmp_path: Path) -> None:
    channel, _ = deciding(
        ApprovalDecision(outcome="approve"),
        ApprovalDecision(outcome="edit", edited_arguments={"text": "buy oat milk"}),
    )
    adapter, _ = scripted_adapter([SAVE, SAVE.model_copy(deep=True), "Saved."], add, save_note)
    script_ctx = RunContext(
        config(tmp_path, guardrails={"max_identical_tool_calls": 5}), approvals=channel
    )
    result = await adapter.run(SPEC, "Save twice", script_ctx)
    assert result.stop_reason == "completed"
    assert NOTES == ["buy milk", "buy oat milk"]


async def test_tools_without_side_effects_need_no_approval(tmp_path: Path) -> None:
    channel, seen = deciding()
    adapter, _ = scripted_adapter([tool_calls(("add", {"a": 1, "b": 2})), "3"], add, save_note)
    await adapter.run(SPEC, "1+2?", RunContext(config(tmp_path), approvals=channel))
    assert seen == []


async def test_a_run_waiting_for_approval_can_be_stored_and_resumed(tmp_path: Path) -> None:
    turn = tool_calls(("add", {"a": 1, "b": 1}), ("save_note", {"text": "two"}), text="Working.")
    channel, _ = deciding(None)  # not decided yet
    adapter, model = scripted_adapter([turn, "All done."], add, save_note)
    first = await adapter.run(SPEC, "Add and save", RunContext(config(tmp_path), approvals=channel))

    assert first.stop_reason == "awaiting_approval" and first.pending is not None
    assert first.pending.request.tool_name == "save_note"
    assert NOTES == [] and len(model.requests) == 1

    stored = first.pending.model_dump_json()  # survives being written down and read back
    pending = PendingApproval.model_validate_json(stored)
    ctx = RunContext(config(tmp_path), run_id=first.run_id)
    second = await adapter.resume(SPEC, pending, ApprovalDecision(outcome="approve"), ctx)

    assert (second.stop_reason, second.output) == ("completed", "All done.")
    assert NOTES == ["two"]
    assert (second.steps, second.tool_calls) == (2, 2)  # counters carry over
    results = model.requests[1].messages[-1].content
    assert [r.content for r in results] == ["2", "saved"]  # type: ignore[union-attr]

    denied_ctx = RunContext(config(tmp_path))
    adapter, _ = scripted_adapter(["Understood."], add, save_note)
    denied = await adapter.resume(SPEC, pending, ApprovalDecision(outcome="deny"), denied_ctx)
    assert denied.stop_reason == "completed" and NOTES == ["two"]


async def test_cli_channel_prompts_the_operator() -> None:
    request = ApprovalRequest(
        run_id="r",
        tool_name="save_note",
        arguments={"text": "x"},
        side_effects="write",
        reason="why",
    )
    prompts: list[str] = []

    def answers(*replies: str) -> CliApproval:
        queue = list(replies)

        def prompt(text: str) -> str:
            prompts.append(text)
            return queue.pop(0)

        return CliApproval(prompt)

    assert (await answers("a").request(request)).outcome == "approve"
    assert "save_note (write)" in prompts[0] and "reason:    why" in prompts[0]
    denied = await answers("d", "too risky").request(request)
    assert (denied.outcome, denied.note) == ("deny", "too risky")
    edited = await answers("e", '{"text": "y"}').request(request)
    assert (edited.outcome, edited.edited_arguments) == ("edit", {"text": "y"})
    assert (await answers("e", "not json").request(request)).outcome == "deny"
    assert await answers("blue").ask("Favourite colour?") == "blue"


def test_channels_come_from_config(tmp_path: Path) -> None:
    assert isinstance(make_channel(config(tmp_path)), CliApproval)
    auto = config(tmp_path, guardrails={"approval_channel": "auto_approve"})
    assert isinstance(make_channel(auto), AutoApprove)
    with pytest.raises(AgentKitError, match="cannot be used when env is 'prod'"):
        AutoApprove("prod")
    with pytest.raises(AgentKitError, match="CallbackApproval must be passed"):
        make_channel(config(tmp_path, guardrails={"approval_channel": "callback"}))


# output schema and checks


class Answer(BaseModel):
    total: int


async def test_output_schema_parses_the_final_answer(tmp_path: Path) -> None:
    spec = SPEC.model_copy(update={"tools": [], "output_schema": Answer})
    adapter, model = scripted_adapter(['{"total": 5}'])
    result = await adapter.run(spec, "2+3?", RunContext(config(tmp_path)))
    assert result.output == Answer(total=5)
    assert model.requests[0].response_schema == Answer.model_json_schema()

    adapter, _ = scripted_adapter(['{"total": "many"}'])
    bad = await adapter.run(spec, "2+3?", RunContext(config(tmp_path)))
    assert bad.stop_reason == "error"
    assert bad.error is not None and bad.error.kind == "invalid_output"


async def test_checks_run_on_input_tool_output_and_final_answer(tmp_path: Path) -> None:
    class NoShouting:
        async def __call__(self, text: str, ctx: RunContext) -> CheckResult:
            return CheckResult(text=text, ok=text != text.upper(), reason="shouting")

    redactor = Redactor(["email"])
    checks = Checks(user_input=[NoShouting()], tool_output=[redactor], final_answer=[redactor])

    @tool(side_effects="none")
    def whois() -> str:
        """Look up the owner."""
        return "owner: ada@example.com"

    spec = SPEC.model_copy(update={"tools": ["whois"]})
    adapter, model = scripted_adapter([tool_calls(("whois", {})), "It is ada@example.com."], whois)
    result = await adapter.run(spec, "who owns it?", RunContext(config(tmp_path), checks=checks))
    assert result.output == "It is [redacted:email]."
    assert model.requests[1].messages[-1].content[0].content == "owner: [redacted:email]"  # type: ignore[union-attr]

    adapter, model = scripted_adapter(["unused"], whois)
    rejected = await adapter.run(spec, "WHO?", RunContext(config(tmp_path), checks=checks))
    assert rejected.stop_reason == "error" and model.requests == []
    assert rejected.error is not None and "shouting" in rejected.error.message

    ctx = RunContext(config(tmp_path))
    assert (await SchemaCheck(Answer)('{"total": 1}', ctx)).ok
    assert not (await SchemaCheck(Answer)("nope", ctx)).ok


# prompts, projects, adapters


async def test_instructions_come_from_a_prompt_file_with_variables(tmp_path: Path) -> None:
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "main.md").write_text("You help {{ company }} customers.\n")
    spec = AgentSpec(
        name="a", description="d", instructions="prompts/main.md", variables={"company": "Acme"}
    )
    adapter, model = scripted_adapter(["ok"], root=tmp_path)
    await adapter.run(spec, "hi", RunContext(config(tmp_path)))
    assert model.requests[0].messages[0].text == "You help Acme customers."

    with pytest.raises(AgentKitError, match="has no 'company'"):
        await adapter.run(
            spec.model_copy(update={"variables": {}}), "hi", RunContext(config(tmp_path))
        )
    missing = spec.model_copy(update={"instructions": "prompts/nope.md"})
    with pytest.raises(AgentKitError, match="does not exist"):
        await adapter.run(missing, "hi", RunContext(config(tmp_path)))


async def test_a_model_without_tool_calling_fails_at_start_up(tmp_path: Path) -> None:
    caps = Capabilities(tools=False, context_window=8192, max_output_tokens=1000)
    provider = FakeProvider(["unused"], capabilities=caps)
    model = Model([Link("local/small", provider, timeout_seconds=5)], RETRY)
    registry = ToolRegistry()
    registry.register(add)
    adapter = NativeAdapter(registry, model_factory=lambda *a, **k: model)
    with pytest.raises(UnsupportedCapability, match="does not support tool calling"):
        await adapter.run(
            SPEC.model_copy(update={"tools": ["add"]}), "hi", RunContext(config(tmp_path))
        )
    assert provider.requests == []


def write_project(root: Path) -> None:
    (root / "tools").mkdir(parents=True)
    (root / "agents").mkdir()
    (root / "tools" / "orders.py").write_text(
        "from agentkit.tools import tool\n\n"
        "@tool(side_effects='none')\n"
        "def lookup_order(order_id: int) -> str:\n"
        '    """Look up an order."""\n'
        "    return 'shipped'\n"
    )
    (root / "agents" / "main.py").write_text(
        "from pydantic import BaseModel\n"
        "from agentkit.runtime.spec import AgentSpec\n"
        "from tools.orders import lookup_order\n\n"
        "class Triage(BaseModel):\n    category: str\n\n"
        "triage = AgentSpec(name='triage', description='d', instructions='Triage.',\n"
        "                   tools=['lookup_order', 'calculator'], output_schema=Triage)\n"
    )


def test_project_loader_collects_agents_tools_and_schemas(tmp_path: Path) -> None:
    write_project(tmp_path / "one")
    cfg = config(tmp_path)
    project = load_project(tmp_path / "one", cfg)
    assert list(project.agents) == ["triage"]
    assert project.registry.get("lookup_order").side_effects == "none"
    assert "calculator" in [t.name for t in project.registry]
    assert project.schemas["Triage"] is project.agents["triage"].output_schema
    with pytest.raises(AgentKitError, match="Defined agents: triage"):
        project.agent("nope")

    # a second project with the same module names does not see the first one's code
    write_project(tmp_path / "two")
    (tmp_path / "two" / "agents" / "main.py").write_text(
        "from agentkit.runtime.spec import AgentSpec\n"
        "other = AgentSpec(name='other', description='d', instructions='x')\n"
    )
    assert list(load_project(tmp_path / "two", cfg).agents) == ["other"]

    (tmp_path / "two" / "agents" / "broken.py").write_text("import nonexistent_module\n")
    with pytest.raises(AgentKitError, match="Could not import .*broken.py"):
        load_project(tmp_path / "two", cfg)


def test_adapters_come_from_config(tmp_path: Path) -> None:
    assert isinstance(get_adapter(config(tmp_path), ToolRegistry()), NativeAdapter)
    with pytest.raises(AgentKitError, match="'langgraph' is not shipped"):
        get_adapter(config(tmp_path, runtime={"adapter": "langgraph"}), ToolRegistry())
