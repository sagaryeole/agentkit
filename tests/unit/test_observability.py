import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentkit.cli.main import app, run_summary
from agentkit.errors import ConfigError
from agentkit.observability.tracer import Tracer, read_trace, render_trace
from agentkit.runtime.context import RunContext
from agentkit.runtime.spec import AgentSpec
from agentkit.tools import tool
from tests.unit.helpers import config, scripted_adapter, tool_calls

PLANTED_KEY = "sk-live-9f8e7d6c5b4a39281706"
PLANTED_ENV_SECRET = "hunter2-correct-horse"


@tool(side_effects="none")
def lookup(name: str) -> str:
    """Look up a customer record.

    Args:
        name: Customer name.
    """
    return f"{name}: contact ada@example.com, token {PLANTED_ENV_SECRET}"


SPEC = AgentSpec(name="support", description="d", instructions="Help.", tools=["lookup"])
SCRIPT = [tool_calls(("lookup", {"name": "Ada"})), f"Her key is {PLANTED_KEY}."]


async def run_traced(tmp_path: Path, **observability: object) -> tuple[Path, list[dict]]:  # type: ignore[type-arg]
    cfg = config(tmp_path, observability={"jsonl_path": str(tmp_path / "traces"), **observability})
    adapter, _ = scripted_adapter(list(SCRIPT), lookup)
    ctx = RunContext(cfg, session_id="s1")
    ctx.tracer = Tracer(cfg, ctx.run_id, session_id="s1", secrets=[PLANTED_ENV_SECRET])
    result = await adapter.run(SPEC, f"Find Ada. My key is {PLANTED_KEY}", ctx)
    assert result.trace_path is not None
    path = Path(result.trace_path)
    return path, read_trace(path)


async def test_every_run_writes_a_trace_with_one_span_per_call(tmp_path: Path) -> None:
    path, spans = await run_traced(tmp_path)
    assert path.parent == tmp_path / "traces" and path.suffix == ".jsonl"
    assert [s["kind"] for s in spans] == ["run", "model_call", "tool_call", "model_call"]
    run = spans[0]
    assert run["parent_id"] is None
    assert all(s["parent_id"] == run["span_id"] for s in spans[1:])
    for span in spans:
        assert span["run_id"] == path.stem
        assert (span["session_id"], span["agent_name"], span["env"]) == ("s1", "support", "dev")
        assert span["duration_ms"] >= 0
    assert run["attributes"]["stop_reason"] == "completed"
    assert (run["attributes"]["steps"], run["attributes"]["tool_calls"]) == (2, 1)
    model_call = spans[1]["attributes"]
    assert model_call["stop_reason"] == "tool_use" and model_call["input_tokens"] == 10
    assert spans[2]["name"] == "lookup"
    assert spans[2]["content"]["arguments"] == {"name": "Ada"}


async def test_planted_secrets_never_reach_the_trace(tmp_path: Path) -> None:
    path, spans = await run_traced(tmp_path)
    written = path.read_text()
    assert PLANTED_KEY not in written  # matched by the api_key pattern
    assert PLANTED_ENV_SECRET not in written  # a known secret value
    assert "ada@example.com" not in written  # matched by the email pattern
    assert "[redacted:api_key]" in written and "[redacted:secret]" in written
    assert "[redacted:email]" in written
    assert "Find Ada." in spans[0]["content"]["input"]  # the rest of the content is kept


async def test_capture_content_false_keeps_metadata_only(tmp_path: Path) -> None:
    path, spans = await run_traced(tmp_path, capture_content=False)
    assert all("content" not in span for span in spans)
    assert "Find Ada" not in path.read_text()
    assert spans[1]["attributes"]["output_tokens"] == 5


async def test_exporters(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = config(tmp_path, observability={"exporter": "none"})
    adapter, _ = scripted_adapter(["hi"])
    spec = SPEC.model_copy(update={"tools": []})
    result = await adapter.run(spec, "hi", RunContext(cfg))
    assert result.trace_path is None and not (tmp_path / ".agentkit").exists()

    console = config(tmp_path, observability={"exporter": "console"})
    adapter, _ = scripted_adapter(["hi"])
    await adapter.run(spec, "hi", RunContext(console))
    lines = capsys.readouterr().err.strip().splitlines()
    assert [json.loads(line)["kind"] for line in lines] == ["model_call", "run"]

    with pytest.raises(ConfigError, match="'otlp' is not shipped yet"):
        RunContext(config(tmp_path, observability={"exporter": "otlp"}))


async def test_a_failing_span_is_marked_as_an_error(tmp_path: Path) -> None:
    cfg = config(tmp_path, observability={"jsonl_path": str(tmp_path / "traces")})
    tracer = Tracer(cfg, "run1", secrets=[])
    with pytest.raises(ValueError), tracer.span("tool_call", "boom"):
        raise ValueError("bad")
    span = read_trace(tmp_path / "traces" / "run1.jsonl")[0]
    assert span["status"] == "error" and span["attributes"]["error"] == "ValueError: bad"


async def test_trace_viewer_and_run_summary(tmp_path: Path) -> None:
    path, spans = await run_traced(tmp_path)
    timeline = render_trace(spans).splitlines()
    assert len(timeline) == 4
    assert "run        support" in timeline[0] and "2 steps, 1 tool calls" in timeline[0]
    assert "model_call fake/scripted" in timeline[1] and "10 in / 5 out" in timeline[1]
    assert "tool_call  lookup" in timeline[2] and '{"name": "Ada"} -> ok' in timeline[2]
    assert render_trace([]) == "The trace is empty."

    runner = CliRunner()
    args = ["--project-dir", str(tmp_path)]
    (tmp_path / "agentkit.yaml").write_text(
        f"observability: {{jsonl_path: '{tmp_path / 'traces'}'}}\n"
    )
    shown = runner.invoke(app, ["trace", path.stem[:8], *args])
    assert shown.exit_code == 0 and "tool_call  lookup" in shown.output
    missing = runner.invoke(app, ["trace", "zzzz", *args])
    assert missing.exit_code == 1 and "no trace matching 'zzzz'" in missing.output


def test_run_command_prints_answer_and_summary(
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
    result = runner.invoke(app, ["run", "echo", "hello", "--project-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "fake: hello" in result.output
    assert "[completed] 1 steps | 0 tool calls" in result.output and "$0.0000" in result.output

    unknown = runner.invoke(app, ["run", "nope", "hello", "--project-dir", str(tmp_path)])
    assert unknown.exit_code == 1 and "No agent named 'nope'" in unknown.output


def test_run_summary_format() -> None:
    from agentkit.models import Usage
    from agentkit.runtime.spec import RunResult

    result = RunResult(
        run_id="abc",
        output="x",
        stop_reason="budget_exceeded",
        usage=Usage(input_tokens=1200, output_tokens=300),
        steps=4,
        tool_calls=6,
        duration_seconds=2.345,
        trace_path="t.jsonl",
    )
    assert run_summary(result) == (
        "[budget_exceeded] 4 steps | 6 tool calls | 1200 in / 300 out tokens | unpriced | 2.3s"
        " | trace: abc"
    )
