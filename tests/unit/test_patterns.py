import asyncio
from pathlib import Path

import pytest

from agentkit.errors import AgentKitError
from agentkit.observability.tracer import read_trace
from agentkit.patterns import (
    FanOut,
    Pattern,
    Plan,
    PlanAndExecute,
    ReviseLoop,
    Verdict,
    Workflow,
    router,
    run_named,
)
from agentkit.runtime.context import RunContext
from agentkit.runtime.project import Project
from agentkit.runtime.spec import AgentSpec
from agentkit.tools import ToolRegistry, tool
from tests.unit.helpers import config, scripted_adapter, tool_calls


@tool(side_effects="none")
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


BILLING = AgentSpec(
    name="billing", description="Handles charges.", instructions="Bill.", tools=["add"]
)
SHIPPING = AgentSpec(name="shipping", description="Handles parcels.", instructions="Ship.")
SUPPORT = router("support", "Routes requests.", routes=["billing", "shipping"])
AGENTS = {a.name: a for a in (BILLING, SHIPPING, SUPPORT)}


def handoff(agent: str, message: str = "the task") -> object:
    return tool_calls(("handoff", {"agent": agent, "message": message}))


async def test_router_hands_the_task_to_the_chosen_agent(tmp_path: Path) -> None:
    script = [
        handoff("billing", "Refund 5 + 7."),
        tool_calls(("add", {"a": 5, "b": 7})),
        "12 owed.",
    ]
    adapter, model = scripted_adapter(script, add)
    adapter.agents = AGENTS
    result = await adapter.run(SUPPORT, "I was charged twice", RunContext(config(tmp_path)))

    assert (result.stop_reason, result.output) == ("completed", "12 owed.")
    assert (result.steps, result.tool_calls) == (3, 2)  # one budget for both agents
    router_request, billing_request, _ = model.requests
    assert [t.name for t in router_request.tools] == ["handoff"]
    listing = router_request.tools[0].description
    assert "- billing: Handles charges." in listing and "- shipping: Handles parcels." in listing
    assert SUPPORT.model == "fast"
    # the specialist starts fresh with its own prompt, tools and the handed-over task
    assert [m.text for m in billing_request.messages] == ["Bill.", "Refund 5 + 7."]
    assert [t.name for t in billing_request.tools] == ["add"]
    roles = [m.role for m in result.messages]
    assert roles == ["system", "user", "assistant", "tool", "system", "user", "assistant", "tool",
                     "assistant"]  # fmt: skip


async def test_handoff_runs_as_a_child_span_of_the_calling_run(tmp_path: Path) -> None:
    adapter, _ = scripted_adapter([handoff("shipping"), "On its way."], add)
    adapter.agents = AGENTS
    result = await adapter.run(SUPPORT, "Where is it?", RunContext(config(tmp_path)))
    assert result.trace_path is not None
    spans = read_trace(Path(result.trace_path))
    runs = [s for s in spans if s["kind"] == "run"]
    assert [(r["name"], r["agent_name"]) for r in runs] == [
        ("support", "support"),
        ("shipping", "shipping"),
    ]
    assert runs[1]["parent_id"] == runs[0]["span_id"]
    assert [s["name"] for s in spans if s["kind"] == "tool_call"] == ["handoff"]


async def test_an_unknown_handoff_target_is_reported_to_the_model(tmp_path: Path) -> None:
    adapter, model = scripted_adapter([handoff("repairs"), "We cannot help with that."], add)
    adapter.agents = AGENTS
    result = await adapter.run(SUPPORT, "Fix my laptop", RunContext(config(tmp_path)))
    assert result.output == "We cannot help with that."
    told = model.requests[1].messages[-1].content[0]
    assert told.is_error and "billing, shipping" in told.content  # type: ignore[union-attr]


async def test_sub_agents_must_be_defined_and_share_the_callers_budget(tmp_path: Path) -> None:
    adapter, _ = scripted_adapter(["unused"], add)
    with pytest.raises(AgentKitError, match="not defined: billing, shipping"):
        await adapter.run(SUPPORT, "hi", RunContext(config(tmp_path)))

    turns = [tool_calls(("add", {"a": i, "b": 0})) for i in range(10)]
    adapter, _ = scripted_adapter([handoff("billing"), *turns], add)
    adapter.agents = AGENTS
    ctx = RunContext(config(tmp_path, limits={"max_steps": 3}))
    result = await adapter.run(SUPPORT, "go", ctx)
    assert result.stop_reason == "budget_exceeded" and result.steps == 3


def project(tmp_path: Path, *patterns: Pattern, **agents: AgentSpec) -> Project:
    proj = Project(root=tmp_path, config=config(tmp_path), registry=ToolRegistry(), agents=agents)
    proj.patterns.update({pattern.name: pattern for pattern in patterns})
    return proj


def agent(name: str, **fields: object) -> AgentSpec:
    return AgentSpec(name=name, description="d", instructions=f"You are {name}.", **fields)  # type: ignore[arg-type]


def run_spans(result: object) -> list[dict]:  # type: ignore[type-arg]
    spans = read_trace(Path(result.trace_path or ""))  # type: ignore[attr-defined]
    return [s for s in spans if s["kind"] == "run"]


# workflow


async def test_workflow_feeds_each_step_into_the_next(tmp_path: Path) -> None:
    flow = Workflow(name="brief", description="d", steps=["first", "second"])
    proj = project(tmp_path, flow, first=agent("first"), second=agent("second"))
    adapter, model = scripted_adapter(["facts", "the brief"])
    result = await run_named(proj, adapter, "brief", "raw notes", RunContext(proj.config))

    assert (result.stop_reason, result.output, result.steps) == ("completed", "the brief", 2)
    assert [r.messages[-1].text for r in model.requests] == ["raw notes", "facts"]
    assert [r.messages[0].text for r in model.requests] == ["You are first.", "You are second."]
    assert len(result.messages) == 6
    runs = run_spans(result)
    assert [r["name"] for r in runs] == ["brief", "first", "second"]
    assert runs[0]["attributes"]["pattern"] == "workflow"
    assert all(r["parent_id"] == runs[0]["span_id"] for r in runs[1:])


async def test_workflow_stops_at_the_first_failing_step(tmp_path: Path) -> None:
    flow = Workflow(name="flow", description="d", steps=["first", "second", "third"])
    agents = {name: agent(name) for name in ("first", "second", "third")}
    proj = project(tmp_path, flow, **agents)
    adapter, model = scripted_adapter(["one"])  # the second step has no reply left
    result = await run_named(proj, adapter, "flow", "in", RunContext(proj.config))
    assert result.stop_reason == "error" and len(model.requests) == 2

    bad = Workflow(name="bad", description="d", steps=["first", "missing"])
    proj.patterns["bad"] = bad
    with pytest.raises(AgentKitError, match="Not defined: missing"):
        await run_named(proj, adapter, "bad", "in", RunContext(proj.config))


async def test_run_named_runs_a_pattern_or_an_agent(tmp_path: Path) -> None:
    flow = Workflow(name="flow", description="d", steps=["only", "only"])
    proj = project(tmp_path, flow, only=agent("only"))
    adapter, _ = scripted_adapter(["a", "b", "c"])
    assert (await run_named(proj, adapter, "flow", "in", RunContext(proj.config))).output == "b"
    assert (await run_named(proj, adapter, "only", "in", RunContext(proj.config))).output == "c"
    proj.require("flow")
    with pytest.raises(AgentKitError, match="No agent named 'nope'"):
        proj.require("nope")


# planner and executor


async def test_the_plan_is_carried_out_step_by_step(tmp_path: Path) -> None:
    solve = PlanAndExecute(name="solve", description="d", planner="planner", executor="doer")
    proj = project(tmp_path, solve, planner=agent("planner"), doer=agent("doer", tools=["add"]))
    script = [
        '{"steps": ["Add 5 and 7", "State the total in a sentence"]}',
        tool_calls(("add", {"a": 5, "b": 7})),
        "12",
        "The total is 12.",
    ]
    adapter, model = scripted_adapter(script, add)
    result = await run_named(proj, adapter, "solve", "What is 5 + 7?", RunContext(proj.config))

    assert (result.stop_reason, result.output) == ("completed", "The total is 12.")
    assert (result.steps, result.tool_calls) == (4, 1)
    planning, first, _, second = model.requests
    assert planning.response_schema == Plan.model_json_schema()  # the planner returns a Plan
    assert first.messages[-1].text == (
        "Overall task:\nWhat is 5 + 7?\n\nYour step now (1 of 2):\nAdd 5 and 7"
    )
    assert "Step 1 result:\n12" in second.messages[-1].text
    assert second.messages[-1].text.endswith("(2 of 2):\nState the total in a sentence")
    runs = run_spans(result)
    assert [r["name"] for r in runs] == ["solve", "planner", "doer", "doer"]
    assert runs[0]["attributes"]["pattern"] == "plan_and_execute"


async def test_bad_plans_and_failing_steps_stop_the_run(tmp_path: Path) -> None:
    solve = PlanAndExecute(
        name="solve", description="d", planner="planner", executor="doer", max_plan_steps=2
    )
    proj = project(tmp_path, solve, planner=agent("planner"), doer=agent("doer"))

    adapter, model = scripted_adapter(['{"steps": ["a", "b", "c"]}'])
    long = await run_named(proj, adapter, "solve", "task", RunContext(proj.config))
    assert long.stop_reason == "error" and len(model.requests) == 1
    assert long.error is not None and long.error.kind == "plan_too_long"

    adapter, _ = scripted_adapter(["no plan here"])
    unplanned = await run_named(proj, adapter, "solve", "task", RunContext(proj.config))
    assert unplanned.error is not None and unplanned.error.kind == "invalid_output"

    adapter, model = scripted_adapter(['{"steps": ["a", "b"]}', "", ""])
    stuck = await run_named(proj, adapter, "solve", "task", RunContext(proj.config))
    assert stuck.stop_reason == "stalled"  # the first step produced nothing; no second step


# parallel fan-out and merge


async def test_workers_run_at_once_and_the_merger_combines_them(tmp_path: Path) -> None:
    review = FanOut(name="review", description="d", workers=["optimist", "critic"], merger="editor")
    agents = {name: agent(name) for name in ("optimist", "critic", "editor")}
    proj = project(tmp_path, review, **agents)
    adapter, model = scripted_adapter(["It will sell.", "It is too expensive.", "Sell it cheaper."])
    result = await run_named(proj, adapter, "review", "Launch the gadget?", RunContext(proj.config))

    assert (result.stop_reason, result.output, result.steps) == ("completed", "Sell it cheaper.", 3)
    assert [r.messages[-1].text for r in model.requests[:2]] == ["Launch the gadget?"] * 2
    assert model.requests[2].messages[-1].text == (
        "Task:\nLaunch the gadget?\n\n"
        "Result from optimist:\nIt will sell.\n\n"
        "Result from critic:\nIt is too expensive."
    )
    runs = run_spans(result)
    assert sorted(r["name"] for r in runs) == ["critic", "editor", "optimist", "review"]
    top = next(r for r in runs if r["name"] == "review")
    assert all(r["parent_id"] == top["span_id"] for r in runs if r is not top)
    # every model call sits under the run of the agent that made it
    spans = read_trace(Path(result.trace_path or ""))
    by_id = {s["span_id"]: s for s in spans}
    calls = [s for s in spans if s["kind"] == "model_call"]
    assert sorted(by_id[c["parent_id"]]["name"] for c in calls) == ["critic", "editor", "optimist"]
    assert sorted(c["agent_name"] for c in calls) == ["critic", "editor", "optimist"]


async def test_workers_really_overlap_and_share_one_budget(tmp_path: Path) -> None:
    running, most = 0, 0

    @tool(side_effects="none")
    async def slow(n: int) -> int:
        """Wait a little."""
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.05)
        running -= 1
        return n

    fan = FanOut(name="fan", description="d", workers=["a", "b", "c"], merger="m")
    agents = {name: agent(name, tools=["slow"]) for name in ("a", "b", "c")}
    proj = project(tmp_path, fan, m=agent("m"), **agents)

    def reply(request: object) -> object:
        last = request.messages[-1]  # type: ignore[attr-defined]
        if last.role == "user" and last.text == "go":
            return tool_calls(("slow", {"n": 1}))
        return "done"

    adapter, _ = scripted_adapter([reply] * 7, slow)
    result = await run_named(proj, adapter, "fan", "go", RunContext(proj.config))
    assert result.stop_reason == "completed" and most == 3
    assert (result.steps, result.tool_calls) == (7, 3)

    adapter, _ = scripted_adapter([reply] * 7, slow)
    tight = RunContext(config(tmp_path, limits={"max_steps": 2}))
    limited = await run_named(proj, adapter, "fan", "go", tight)
    assert limited.stop_reason == "budget_exceeded"


async def test_a_failing_worker_stops_the_fan_out_before_the_merge(tmp_path: Path) -> None:
    fan = FanOut(name="fan", description="d", workers=["a", "b"], merger="m")
    proj = project(tmp_path, fan, a=agent("a"), b=agent("b"), m=agent("m"))
    adapter, model = scripted_adapter(["fine", "", ""])
    result = await run_named(proj, adapter, "fan", "go", RunContext(proj.config))
    assert (
        result.stop_reason == "stalled" and len(model.requests) == 3
    )  # b was asked twice; no merge


# evaluator and reviser


async def test_a_draft_is_revised_until_approved(tmp_path: Path) -> None:
    polish = ReviseLoop(name="polish", description="d", writer="writer", evaluator="reviewer")
    proj = project(tmp_path, polish, writer=agent("writer"), reviewer=agent("reviewer"))
    script = [
        "first draft",
        '{"approved": false, "feedback": "Too vague."}',
        "second draft",
        '{"approved": true}',
    ]
    adapter, model = scripted_adapter(script)
    result = await run_named(
        proj, adapter, "polish", "Describe the launch", RunContext(proj.config)
    )

    assert (result.stop_reason, result.output, result.steps) == ("completed", "second draft", 4)
    _, review, revision, _ = model.requests
    assert review.response_schema == Verdict.model_json_schema()
    assert review.messages[-1].text == "Task:\nDescribe the launch\n\nDraft to review:\nfirst draft"
    for expected in ("Your previous draft:\nfirst draft", "Reviewer feedback:\nToo vague."):
        assert expected in revision.messages[-1].text
    runs = run_spans(result)
    assert [r["name"] for r in runs] == ["polish", "writer", "reviewer", "writer", "reviewer"]


async def test_the_loop_is_bounded_and_approval_ends_it_early(tmp_path: Path) -> None:
    proj = project(tmp_path, writer=agent("writer"), reviewer=agent("reviewer"))
    no = '{"approved": false, "feedback": "Again."}'

    proj.patterns["two"] = ReviseLoop(
        name="two", description="d", writer="writer", evaluator="reviewer", max_rounds=2
    )
    adapter, model = scripted_adapter(["d1", no, "d2", no, "never written"])
    bounded = await run_named(proj, adapter, "two", "task", RunContext(proj.config))
    assert bounded.output == "d2" and len(model.requests) == 4  # two reviews, one revision

    proj.patterns["one"] = ReviseLoop(
        name="one", description="d", writer="writer", evaluator="reviewer"
    )
    adapter, model = scripted_adapter(["good draft", '{"approved": true}'])
    quick = await run_named(proj, adapter, "one", "task", RunContext(proj.config))
    assert quick.output == "good draft" and len(model.requests) == 2

    adapter, _ = scripted_adapter(["draft", "looks fine to me"])
    garbled = await run_named(proj, adapter, "one", "task", RunContext(proj.config))
    assert garbled.error is not None and garbled.error.kind == "invalid_output"
