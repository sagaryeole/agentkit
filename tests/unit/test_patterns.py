from pathlib import Path

import pytest

from agentkit.errors import AgentKitError
from agentkit.observability.tracer import read_trace
from agentkit.patterns import Workflow, router, run_named, run_workflow
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


def project(tmp_path: Path, **agents: AgentSpec) -> Project:
    return Project(root=tmp_path, config=config(tmp_path), registry=ToolRegistry(), agents=agents)


async def test_workflow_feeds_each_step_into_the_next(tmp_path: Path) -> None:
    first = AgentSpec(name="first", description="d", instructions="Extract.")
    second = AgentSpec(name="second", description="d", instructions="Write.")
    flow = Workflow(name="brief", description="d", steps=["first", "second"])
    proj = project(tmp_path, first=first, second=second)
    adapter, model = scripted_adapter(["facts", "the brief"])
    ctx = RunContext(proj.config)
    result = await run_workflow(flow, "raw notes", adapter, proj, ctx)

    assert (result.stop_reason, result.output, result.steps) == ("completed", "the brief", 2)
    assert [r.messages[-1].text for r in model.requests] == ["raw notes", "facts"]
    assert [r.messages[0].text for r in model.requests] == ["Extract.", "Write."]
    assert len(result.messages) == 6

    spans = read_trace(Path(result.trace_path or ""))
    runs = [s for s in spans if s["kind"] == "run"]
    assert [r["name"] for r in runs] == ["brief", "first", "second"]
    assert runs[0]["attributes"]["pattern"] == "workflow"
    assert all(r["parent_id"] == runs[0]["span_id"] for r in runs[1:])


async def test_workflow_stops_at_the_first_failing_step(tmp_path: Path) -> None:
    first = AgentSpec(name="first", description="d", instructions="x")
    second = AgentSpec(name="second", description="d", instructions="y")
    third = AgentSpec(name="third", description="d", instructions="z")
    flow = Workflow(name="flow", description="d", steps=["first", "second", "third"])
    proj = project(tmp_path, first=first, second=second, third=third)
    adapter, model = scripted_adapter(["one"])  # the second step has no reply left
    result = await run_workflow(flow, "in", adapter, proj, RunContext(proj.config))
    assert result.stop_reason == "error" and len(model.requests) == 2

    with pytest.raises(AgentKitError, match="Not defined: missing"):
        bad = Workflow(name="bad", description="d", steps=["first", "missing"])
        await run_workflow(bad, "in", adapter, proj, RunContext(proj.config))


async def test_run_named_runs_a_workflow_or_an_agent(tmp_path: Path) -> None:
    only = AgentSpec(name="only", description="d", instructions="x")
    proj = project(tmp_path, only=only)
    proj.workflows["flow"] = Workflow(name="flow", description="d", steps=["only", "only"])
    adapter, _ = scripted_adapter(["a", "b", "c"])
    assert (await run_named(proj, adapter, "flow", "in", RunContext(proj.config))).output == "b"
    assert (await run_named(proj, adapter, "only", "in", RunContext(proj.config))).output == "c"
    proj.require("flow")
    with pytest.raises(AgentKitError, match="No agent named 'nope'"):
        proj.require("nope")
