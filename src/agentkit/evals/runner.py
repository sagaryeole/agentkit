"""Run eval suites: each case through the agent, then through its graders."""

import asyncio
import functools
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from agentkit.config import Config
from agentkit.errors import AgentKitError
from agentkit.evals.cassette import RecordingProvider, ReplayProvider
from agentkit.evals.dataset import Case, FakeTurn, Suite
from agentkit.evals.fakes import EvalApprovals, eval_registry, load_fixtures
from agentkit.evals.graders import (
    JUDGE_PROMPT_VERSION,
    GradeContext,
    GradeResult,
    called_tools,
    grade,
)
from agentkit.models.base import (
    ContentPart,
    Message,
    ModelProvider,
    ModelResponse,
    TextPart,
    ToolCallPart,
    Usage,
)
from agentkit.models.model import Link, Model
from agentkit.models.providers.fake import FakeProvider
from agentkit.models.registry import build_provider, get_model
from agentkit.runtime.adapters.native import NativeAdapter
from agentkit.runtime.context import RunContext
from agentkit.runtime.project import Project
from agentkit.runtime.spec import ErrorInfo, RunResult

CassetteMode = Literal["record", "replay"] | None


class Attempt(BaseModel):
    run_id: str
    passed: bool
    stop_reason: str
    grades: list[GradeResult]
    output: Any = None
    error: ErrorInfo | None = None
    tools_called: list[str] = []
    steps: int = 0
    tokens: int = 0
    cost_usd: float | None = None
    duration_seconds: float = 0.0
    trace_path: str | None = None


class CaseResult(BaseModel):
    id: str
    tags: list[str] = []
    attempts: list[Attempt]

    @property
    def pass_rate(self) -> float:
        return sum(a.passed for a in self.attempts) / len(self.attempts)


class SuiteResult(BaseModel):
    suite: str
    agent: str
    cases: list[CaseResult]


class EvalRun(BaseModel):
    id: str
    created: str
    repeat: int
    live: bool
    cassette: CassetteMode
    judge_prompt_version: str = JUDGE_PROMPT_VERSION
    suites: list[SuiteResult]


def _fake_script(turns: list[FakeTurn]) -> list[str | ModelResponse]:
    script: list[str | ModelResponse] = []
    for turn in turns:
        calls = [*([turn.tool_call] if turn.tool_call else []), *turn.tool_calls]
        if not calls:
            script.append(turn.text)
            continue
        content: list[ContentPart] = [TextPart(text=turn.text)] if turn.text else []
        content += [
            ToolCallPart(id=f"call_{len(script)}_{i}", name=c.name, arguments=c.arguments)
            for i, c in enumerate(calls)
        ]
        script.append(
            ModelResponse(
                message=Message(role="assistant", content=content),
                stop_reason="tool_use",
                usage=Usage(input_tokens=0, output_tokens=0),
                model_id="fake",
            )
        )
    return script


def _provider_factory(cassette: CassetteMode, path: Path) -> Any:
    if cassette is None:
        return None

    def factory(cfg: Config, key: str, environ: Mapping[str, str] | None) -> ModelProvider:
        if cassette == "replay":
            return ReplayProvider(key, path)
        return RecordingProvider(key, path, build_provider(cfg, key, environ))

    return factory


async def _attempt(
    suite: Suite,
    case: Case,
    project: Project,
    *,
    live: bool,
    cassette: CassetteMode,
    cassette_path: Path,
) -> Attempt:
    cfg = project.config
    factory = _provider_factory(cassette, cassette_path)
    scripted = case.setup.fake_model is not None and cassette is None

    def model_factory(name: str, cfg: Config, **kwargs: Any) -> Model:
        if scripted:
            provider = FakeProvider(_fake_script(case.setup.fake_model or []))
            return Model([Link("fake/scripted", provider, timeout_seconds=60)], cfg.retry)
        return get_model(name, cfg, provider_factory=factory, **kwargs)

    fixture_dir = (suite.path.parent if suite.path else project.root) / "fixtures"
    registry = eval_registry(
        project.registry, load_fixtures(fixture_dir, case.setup.fixtures), live=live
    )
    ctx = RunContext(cfg, approvals=EvalApprovals(case.setup.approve_tools))
    adapter = NativeAdapter(registry, root=project.root, model_factory=model_factory)
    try:
        result = await adapter.run(project.agent(suite.agent), case.input, ctx)
    except AgentKitError as exc:
        result = RunResult(
            run_id=ctx.run_id,
            output=None,
            stop_reason="error",
            usage=Usage(input_tokens=0, output_tokens=0),
            steps=0,
            error=ErrorInfo(kind=type(exc).__name__, message=str(exc)),
        )

    def judge() -> Model:
        if scripted:
            return Model([Link("fake/judge", FakeProvider(), timeout_seconds=60)], cfg.retry)
        return get_model(cfg.evals.judge_role, cfg, provider_factory=factory)

    grade_ctx = GradeContext(project=project, judge=functools.cache(judge))
    grades = [
        await grade(case, result, spec.type, spec.params, grade_ctx)
        for spec in suite.graders_for(case)
    ]
    return Attempt(
        run_id=result.run_id,
        passed=all(g.passed for g in grades if not g.skipped),
        stop_reason=result.stop_reason,
        grades=grades,
        output=to_jsonable_python(result.output),
        error=result.error,
        tools_called=[call.name for call in called_tools(result)],
        steps=result.steps,
        tokens=result.usage.input_tokens + result.usage.output_tokens,
        cost_usd=result.usage.cost_usd,
        duration_seconds=result.duration_seconds,
        trace_path=result.trace_path,
    )


async def run_suite(
    suite: Suite,
    project: Project,
    *,
    repeat: int = 1,
    tags: list[str] | None = None,
    live: bool = False,
    cassette: CassetteMode = None,
) -> SuiteResult:
    """Run every case (or those with any of `tags`) `repeat` times, concurrently up to
    `evals.concurrency`. Tools are fakes unless `live`; side-effecting tools are denied
    unless the case allows them."""
    cfg = project.config
    project.agent(suite.agent)  # fail early if the suite names an unknown agent
    cassette_path = Path(cfg.evals.cassette_dir) / f"{suite.suite}.jsonl"
    if cassette == "record" and cassette_path.exists():
        cassette_path.unlink()
    cases = [c for c in suite.cases if not tags or set(tags) & set(c.tags)]
    limit = asyncio.Semaphore(cfg.evals.concurrency)

    async def bounded(case: Case) -> Attempt:
        async with limit:
            return await _attempt(
                suite, case, project, live=live, cassette=cassette, cassette_path=cassette_path
            )

    attempts = await asyncio.gather(*(bounded(case) for case in cases for _ in range(repeat)))
    results = [
        CaseResult(
            id=case.id, tags=case.tags, attempts=list(attempts[i * repeat : (i + 1) * repeat])
        )
        for i, case in enumerate(cases)
    ]
    return SuiteResult(suite=suite.suite, agent=suite.agent, cases=results)


async def run_suites(
    suites: list[Suite],
    project: Project,
    *,
    repeat: int = 1,
    tags: list[str] | None = None,
    live: bool = False,
    cassette: CassetteMode = None,
) -> EvalRun:
    results = [
        await run_suite(s, project, repeat=repeat, tags=tags, live=live, cassette=cassette)
        for s in suites
    ]
    now = datetime.now(UTC)
    return EvalRun(
        id=f"{now:%Y%m%d-%H%M%S-%f}",  # sorts by time, so `latest` is the last file
        created=now.isoformat(timespec="seconds"),
        repeat=repeat,
        live=live,
        cassette=cassette,
        suites=results,
    )


def save_run(run: EvalRun, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{run.id}.json"
    path.write_text(run.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_run(reference: str, directory: Path) -> EvalRun:
    """A saved run by file path, by ID or the start of one, or `latest`."""
    path = Path(reference)
    if not path.is_file():
        pattern = "*.json" if reference == "latest" else f"{reference}*.json"
        matches = sorted(directory.glob(pattern))
        if not matches or (len(matches) > 1 and reference != "latest"):
            raise AgentKitError(
                f"Found {len(matches)} eval runs matching '{reference}' in {directory}. "
                "Give a run ID, a file path, or 'latest'."
            )
        path = matches[-1]
    return EvalRun.model_validate_json(path.read_text(encoding="utf-8"))
