import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agentkit.cli.main import app
from agentkit.errors import AgentKitError, ModelError
from agentkit.evals.cassette import RecordingProvider, ReplayProvider
from agentkit.evals.dataset import Case, load_suite, load_suites
from agentkit.evals.graders import GradeContext, GradeResult, grade
from agentkit.evals.report import compare, render_comparison, render_report
from agentkit.evals.runner import EvalRun, load_run, run_suites, save_run
from agentkit.models import Message, ModelRequest, Usage
from agentkit.models.base import Capabilities
from agentkit.models.model import Link, Model
from agentkit.models.providers.fake import FakeProvider
from agentkit.runtime.project import Project, load_project
from agentkit.runtime.spec import ErrorInfo, RunResult
from tests.unit.helpers import RETRY, config, tool_calls

REPO = Path(__file__).parents[2]

AGENTS = '''
from pydantic import BaseModel
from agentkit.runtime.spec import AgentSpec
from agentkit.tools import tool

SENT = []

class Triage(BaseModel):
    category: str
    order: int

@tool(side_effects="read_external")
def lookup_order(order_id: int) -> dict:
    """Look up an order.

    Args:
        order_id: The order number.
    """
    raise RuntimeError("the real order system must not be called in evals")

@tool
def issue_refund(order_id: int) -> str:
    """Refund an order.

    Args:
        order_id: The order number.
    """
    SENT.append(order_id)
    return "refunded"

triage = AgentSpec(name="triage_agent", description="d", instructions="Triage.",
                   tools=["lookup_order", "issue_refund", "calculator"])
'''
GRADERS = """
from agentkit.evals.graders import GradeResult

def polite(case, result):
    return GradeResult(passed="please" in str(result.output), reason="not polite")
"""
FIXTURE = """
tools:
  lookup_order:
    - when: {order_id: 1182}
      returns: {id: 1182, status: charged_twice}
    - error: {kind: not_found, message: "No such order."}
"""
SUITE = """
suite: support_triage
agent: triage_agent
defaults:
  graders: [no_error, within_budget]
cases:
  - id: refund_simple
    input: "I was charged twice for order 1182."
    setup:
      fixtures: [orders_small]
      fake_model:
        - text: "Let me look."
          tool_call: {name: lookup_order, arguments: {order_id: 1182}}
        - text: '{"category": "billing", "order": 1182}'
    expect:
      tool_calls:
        must_include: [lookup_order]
        must_not_include: [issue_refund]
      output:
        contains: ["1182"]
        schema: Triage
        fields: {category: billing}
    graders:
      - type: llm_judge
        rubric: "Reply names the order."
    tags: [billing, smoke]
  - id: refund_denied_without_approval
    input: "Refund order 7."
    setup:
      fake_model:
        - tool_call: {name: issue_refund, arguments: {order_id: 7}}
        - text: "I could not refund it."
    expect:
      output: {contains: ["could not"]}
    tags: [refunds]
  - id: unknown_order
    input: "Where is order 5?"
    setup:
      fixtures: [orders_small]
      fake_model:
        - tool_call: {name: lookup_order, arguments: {order_id: 5}}
        - text: "Order 5 was not found."
    expect:
      output: {regex: "not found"}
"""


@pytest.fixture
def project(tmp_path: Path) -> Project:
    root = tmp_path / "proj"
    (root / "agents").mkdir(parents=True)
    (root / "evals" / "fixtures").mkdir(parents=True)
    (root / "agents" / "main.py").write_text(AGENTS)
    (root / "my_graders.py").write_text(GRADERS)
    (root / "evals" / "fixtures" / "orders_small.yaml").write_text(FIXTURE)
    (root / "evals" / "support.yaml").write_text(SUITE)
    return load_project(root, config(tmp_path, tools={"enabled": ["calculator"]}))


# dataset


def test_suite_loads_and_expect_becomes_graders(project: Project) -> None:
    suite = load_suite(project.root / "evals" / "support.yaml")
    assert (suite.suite, suite.agent, len(suite.cases)) == ("support_triage", "triage_agent", 3)
    specs = suite.graders_for(suite.cases[0])
    assert [s.type for s in specs] == [
        "no_error", "within_budget", "tool_calls", "contains", "schema", "fields", "llm_judge",
    ]  # fmt: skip
    assert specs[2].params["must_not_include"] == ["issue_refund"]
    assert specs[6].params == {"rubric": "Reply names the order."}
    assert [s.suite for s in load_suites(project.root / "evals")] == ["support_triage"]


def test_jsonl_suites_and_invalid_suites(tmp_path: Path) -> None:
    rows = [
        {"suite": "s", "agent": "a"},
        {"id": "one", "input": "hi", "graders": ["no_error"]},
        {"id": "two", "input": "ho"},
    ]
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows))
    assert [c.id for c in load_suite(path).cases] == ["one", "two"]

    bad = tmp_path / "bad.yaml"
    bad.write_text("suite: s\nagent: a\ncases: [{id: x, input: hi, expectt: {}}]\n")
    with pytest.raises(AgentKitError, match="cases.0.expectt"):
        load_suite(bad)
    bad.write_text("suite: s\nagent: a\ncases: [{id: x, input: a}, {id: x, input: b}]\n")
    with pytest.raises(AgentKitError, match="repeated: x"):
        load_suite(bad)
    with pytest.raises(AgentKitError, match="No eval suites"):
        load_suites(tmp_path / "nowhere")


# graders


def finished(output: Any = "The total is 42.", **changes: Any) -> RunResult:
    base: dict[str, Any] = {
        "run_id": "r",
        "output": output,
        "stop_reason": "completed",
        "usage": Usage(input_tokens=100, output_tokens=50, cost_usd=0.01),
        "steps": 2,
        "duration_seconds": 1.5,
        "messages": [
            tool_calls(("search", {"q": "x", "n": 3})).message,
            tool_calls(("fetch", {"url": "u"}), ("search", {"q": "y"})).message,
        ],
    }
    return RunResult(**{**base, **changes})


CASE = Case(id="c", input="What is the total?")


async def graded(project: Project, kind: str, result: RunResult, **params: Any) -> GradeResult:
    def judge() -> Model:
        raise AssertionError("no judge expected")

    return await grade(CASE, result, kind, params, GradeContext(project=project, judge=judge))


async def test_text_graders(project: Project) -> None:
    result = finished()
    assert (await graded(project, "exact", result, value="The total is 42.")).passed
    assert not (await graded(project, "exact", result, value="42")).passed
    assert (await graded(project, "contains", result, values=["total", "42"])).passed
    missing = await graded(project, "contains", result, values=["42", "euros"])
    assert not missing.passed and "euros" in missing.reason
    assert (await graded(project, "regex", result, pattern=r"\b\d+\b")).passed
    assert not (await graded(project, "regex", result, pattern=r"^\d+$")).passed


async def test_schema_and_fields_graders(project: Project) -> None:
    good = finished('{"category": "billing", "order": 7}')
    assert (await graded(project, "schema", good, name="Triage")).passed
    assert (await graded(project, "fields", good, fields={"category": "billing"})).passed
    wrong = await graded(project, "fields", good, fields={"category": "shipping"})
    assert not wrong.passed and "billing" in wrong.reason
    structured = finished(project.schemas["Triage"](category="billing", order=7))
    assert (await graded(project, "fields", structured, fields={"order": 7})).passed
    assert not (await graded(project, "schema", finished('{"order": 7}'), name="Triage")).passed
    assert not (await graded(project, "fields", finished("plain"), fields={"a": 1})).passed
    with pytest.raises(AgentKitError, match="Known: Triage"):
        await graded(project, "schema", good, name="Nope")


async def test_tool_call_and_trajectory_graders(project: Project) -> None:
    result = finished()
    ok = await graded(
        project,
        "tool_calls",
        result,
        must_include=["fetch", {"name": "search", "arguments": {"q": "x"}}],
        must_not_include=["delete"],
        in_order=["search", "fetch", "search"],
    )
    assert ok.passed
    bad = await graded(
        project,
        "tool_calls",
        result,
        must_include=[{"name": "search", "arguments": {"q": "z"}}],
        must_not_include=["fetch"],
        in_order=["fetch", "fetch"],
    )
    assert not bad.passed
    for expected in ("missing call", "forbidden call fetch", "not in order"):
        assert expected in bad.reason
    sequence = ["search", "fetch", "search"]
    assert (await graded(project, "trajectory", result, tool_sequence=sequence, max_steps=2)).passed
    assert not (await graded(project, "trajectory", result, tool_sequence=["search"])).passed
    assert not (await graded(project, "trajectory", result, max_steps=1)).passed


async def test_run_outcome_graders(project: Project) -> None:
    result = finished()
    assert (await graded(project, "no_error", result)).passed
    failed = finished(stop_reason="error", error=ErrorInfo(kind="x", message="it broke"))
    assert "it broke" in (await graded(project, "no_error", failed)).reason
    limits = {"max_steps": 2, "max_tokens": 150, "max_cost_usd": 0.01, "max_seconds": 2}
    assert (await graded(project, "within_budget", result, **limits)).passed
    over = await graded(project, "within_budget", result, max_tokens=149, max_seconds=1)
    assert not over.passed and "max_tokens 150 > 149" in over.reason
    assert not (
        await graded(project, "within_budget", finished(stop_reason="budget_exceeded"))
    ).passed
    with pytest.raises(AgentKitError, match="does not know"):
        await graded(project, "within_budget", result, max_parsecs=1)


async def test_llm_judge_scores_against_a_rubric(project: Project) -> None:
    async def judged(reply: str, **params: Any) -> tuple[GradeResult, FakeProvider]:
        provider = FakeProvider([reply])
        model = Model([Link("anthropic/judge", provider, timeout_seconds=5)], RETRY)
        model.chain[0].provider.name = "anthropic"  # type: ignore[misc]
        ctx = GradeContext(project=project, judge=lambda: model)
        params = {"rubric": "States the total.", **params}
        return await grade(CASE, finished(), "llm_judge", params, ctx), provider

    passed, provider = await judged('{"score": 0.9, "reason": "States 42."}')
    assert (passed.passed, passed.score, passed.reason) == (True, 0.9, "States 42.")
    request = provider.requests[0]
    assert request.response_schema is not None  # the judge returns structured output
    assert "Rubric: States the total." in request.messages[1].text
    assert "What is the total?" in request.messages[1].text
    assert "The total is 42." in request.messages[1].text
    assert "grade the output" in request.messages[0].text

    low, _ = await judged('{"score": 0.4, "reason": "Vague."}')
    assert not low.passed
    lenient, _ = await judged('{"score": 0.4, "reason": "Vague."}', min_score=0.3)
    assert lenient.passed
    garbled, _ = await judged("I think it is fine")
    assert not garbled.passed and "did not return a score" in garbled.reason


async def test_custom_and_unknown_graders(project: Project) -> None:
    polite = await graded(project, "custom", finished("yes please"), function="my_graders:polite")
    assert polite.passed and polite.grader == "custom"
    assert not (
        await graded(project, "custom", finished("no"), function="my_graders:polite")
    ).passed
    with pytest.raises(AgentKitError, match="could not be loaded"):
        await graded(project, "custom", finished(), function="my_graders:missing")
    with pytest.raises(AgentKitError, match="unknown grader 'vibes'"):
        await graded(project, "vibes", finished())
    with pytest.raises(AgentKitError, match="needs the parameter 'value'"):
        await graded(project, "exact", finished())


# runner


async def test_suite_runs_on_fakes_with_no_network(project: Project) -> None:
    suites = load_suites(project.root / "evals")
    run = await run_suites(suites, project)
    cases = {c.id: c for c in run.suites[0].cases}
    assert all(c.pass_rate == 1 for c in cases.values())

    simple = cases["refund_simple"].attempts[0]
    assert simple.tools_called == ["lookup_order"] and simple.steps == 2
    assert simple.output == '{"category": "billing", "order": 1182}'
    judge = next(g for g in simple.grades if g.grader == "llm_judge")
    assert judge.skipped  # the fake provider cannot judge
    assert simple.trace_path is not None and Path(simple.trace_path).is_file()

    # the side-effecting tool was denied, the model was told, and the run went on
    denied = cases["refund_denied_without_approval"].attempts[0]
    assert denied.tools_called == ["issue_refund"] and denied.stop_reason == "completed"
    import agents.main as project_agents  # the project's own module

    assert project_agents.SENT == []


async def test_cases_can_allow_a_side_effecting_tool_and_live_uses_real_tools(
    project: Project,
) -> None:
    import agents.main as project_agents

    suite = load_suite(project.root / "evals" / "support.yaml")
    refund = suite.cases[1]
    refund.setup.approve_tools = ["issue_refund"]
    refund.expect = None
    suite.cases = [refund]
    await run_suites([suite], project, live=True)
    assert project_agents.SENT == [7]

    # without --live, an external tool with no fixture reports that instead of running
    lookup = load_suite(project.root / "evals" / "support.yaml").cases[2]
    lookup.setup.fixtures = []
    lookup.expect = None
    suite.cases = [lookup]
    run = await run_suites([suite], project)
    assert run.suites[0].cases[0].attempts[0].stop_reason == "completed"


async def test_repeat_tags_and_unknown_agent(project: Project) -> None:
    suites = load_suites(project.root / "evals")
    run = await run_suites(suites, project, repeat=3, tags=["smoke", "refunds"])
    assert [c.id for c in run.suites[0].cases] == [
        "refund_simple",
        "refund_denied_without_approval",
    ]
    assert all(len(c.attempts) == 3 and c.pass_rate == 1 for c in run.suites[0].cases)

    suites[0].agent = "nobody"
    with pytest.raises(AgentKitError, match="No agent named 'nobody'"):
        await run_suites(suites, project)


async def test_a_failing_case_is_reported_with_reasons(project: Project) -> None:
    suite = load_suite(project.root / "evals" / "support.yaml")
    suite.cases = [suite.cases[2]]
    suite.cases[0].expect.output.regex = "delivered"  # type: ignore[union-attr]
    run = await run_suites([suite], project, repeat=2)
    report = render_report(run)
    assert "pass rate: 0.0% (0/2 attempts, 1 cases)" in report
    assert "unknown_order (0/2 passed)" in report
    assert "regex: output does not match /delivered/" in report
    assert "no_error       100.0%" in report and "steps:   mean 2.0, p95 2.0" in report


# record and replay


async def test_responses_are_recorded_and_replayed(tmp_path: Path) -> None:
    request = ModelRequest(messages=[Message.from_text("user", "hello")])
    caps = Capabilities(structured_output=False, context_window=8192, max_output_tokens=512)
    cassette = tmp_path / "cassettes" / "s.jsonl"
    live = FakeProvider(["recorded answer"], capabilities=caps)
    recorded = await RecordingProvider("local/default", cassette, live).generate(request)
    assert recorded.message.text == "recorded answer"

    replay = ReplayProvider("local/default", cassette)
    assert (await replay.generate(request)).message.text == "recorded answer"
    assert replay.capabilities == caps  # replay behaves like the recorded model
    assert len(live.requests) == 1

    changed = ModelRequest(messages=[Message.from_text("user", "something else")])
    with pytest.raises(ModelError, match="record again with `agentkit eval run --record`"):
        await replay.generate(changed)
    with pytest.raises(ModelError, match="no recorded response"):
        await ReplayProvider("other/model", cassette).generate(request)


async def test_eval_run_records_then_replays_without_the_provider(
    project: Project, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = project.config.model_copy(deep=True)
    cfg.models.roles["default"] = "fake/default"
    cfg.models.roles["judge"] = "fake/default"
    cfg.evals.cassette_dir = str(tmp_path / "cassettes")
    project.config = cfg
    suite = load_suite(project.root / "evals" / "support.yaml")
    suite.cases = [Case(id="echo", input="ping", graders=["no_error"])]

    recorded = await run_suites([suite], project, cassette="record")
    assert recorded.suites[0].cases[0].attempts[0].output == "fake: ping"
    assert (tmp_path / "cassettes" / "support_triage.jsonl").is_file()

    def no_provider(*args: Any) -> None:
        raise AssertionError("replay must not build the real provider")

    monkeypatch.setattr("agentkit.evals.runner.build_provider", no_provider)
    replayed = await run_suites([suite], project, cassette="replay")
    assert replayed.suites[0].cases[0].attempts[0].output == "fake: ping"

    suite.cases = [Case(id="echo", input="a new question", graders=["no_error"])]
    missed = await run_suites([suite], project, cassette="replay")
    attempt = missed.suites[0].cases[0].attempts[0]
    assert not attempt.passed and "no recorded response" in attempt.grades[0].reason


# results, compare, CLI


async def test_results_are_saved_and_compared(project: Project, tmp_path: Path) -> None:
    suites = load_suites(project.root / "evals")
    baseline = await run_suites(suites, project)
    path = save_run(baseline, tmp_path / "results")
    assert load_run(baseline.id[:15], tmp_path / "results").id == baseline.id
    assert load_run(str(path), tmp_path).id == baseline.id
    assert load_run("latest", tmp_path / "results").id == baseline.id
    with pytest.raises(AgentKitError, match="Found 0 eval runs"):
        load_run("nope", tmp_path / "results")
    assert json.loads(path.read_text())["suites"][0]["cases"][0]["attempts"][0]["grades"]

    suites[0].cases[2].expect.output.regex = "delivered"  # type: ignore[union-attr]
    suites[0].cases.append(Case(id="new_case", input="x", setup={"fake_model": [{"text": "y"}]}))  # type: ignore[arg-type]
    candidate = await run_suites(suites, project)
    result = compare(baseline, candidate)
    assert result.regressions == ["support_triage/unknown_order: 100% -> 0%"]
    assert result.added == ["support_triage/new_case"] and result.improvements == []
    assert result.drop == pytest.approx(25.0)
    text = render_comparison(baseline, candidate, result)
    assert "(-25.0 points)" in text and "Regressions:" in text

    back = compare(candidate, baseline)
    assert back.improvements == ["support_triage/unknown_order: 0% -> 100%"]
    assert "No case changed." in render_comparison(baseline, baseline, compare(baseline, baseline))


@pytest.mark.parametrize("example", ["single_agent", "builtin_tools"])
def test_example_suites_pass_with_no_network_and_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, example: str
) -> None:
    for name in ("MODEL_PROVIDER", "ANTHROPIC_API_KEY", "AGENTKIT_ENV"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / example
    shutil.copytree(REPO / "examples" / example, root, ignore=shutil.ignore_patterns(".agentkit"))
    runner = CliRunner()
    result = runner.invoke(app, ["eval", "run", "--tags", "smoke", "--project-dir", str(root)])
    assert result.exit_code == 0, result.output
    assert "pass rate: 100.0%" in result.output and "Results:" in result.output

    shown = runner.invoke(app, ["eval", "report", "--project-dir", str(root)])
    assert shown.exit_code == 0 and "pass rate: 100.0%" in shown.output


def test_eval_cli_thresholds(
    project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    root = project.root
    (root / "agentkit.yaml").write_text("tools: {enabled: [calculator]}\n")
    runner = CliRunner()
    args = ["--project-dir", str(root)]
    good = runner.invoke(app, ["eval", "run", *args])
    assert good.exit_code == 0, good.output
    baseline = sorted((tmp_path / ".agentkit" / "evals").glob("*.json"))[0].stem

    suite = (
        (root / "evals" / "support.yaml").read_text().replace('regex: "not found"', "regex: nope")
    )
    (root / "evals" / "support.yaml").write_text(suite)
    bad = runner.invoke(app, ["eval", "run", *args])
    assert bad.exit_code == 1 and "Pass rate 66.7% is below 100%" in bad.output
    assert runner.invoke(app, ["eval", "run", "--fail-under", "60", *args]).exit_code == 0

    compared = runner.invoke(app, ["eval", "compare", baseline, "latest", *args])
    assert compared.exit_code == 1 and "more than the 2 allowed" in compared.output
    lenient = runner.invoke(app, ["eval", "compare", baseline, "latest", "--max-drop", "50", *args])
    assert lenient.exit_code == 0
    none = runner.invoke(app, ["eval", "run", "--tags", "nonexistent", *args])
    assert none.exit_code == 1 and "No cases matched" in none.output
    both = runner.invoke(app, ["eval", "run", "--record", "--replay", *args])
    assert both.exit_code == 1


def test_eval_run_model_roundtrip() -> None:
    run = EvalRun(id="x", created="now", repeat=1, live=False, cassette=None, suites=[])
    assert EvalRun.model_validate_json(run.model_dump_json()) == run
