"""Graders: each checks one thing about a finished run and returns a GradeResult."""

import importlib
import inspect
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError
from pydantic_core import to_jsonable_python

from agentkit.errors import AgentKitError, ModelError
from agentkit.evals.dataset import Case
from agentkit.models.base import Message, ModelRequest, ToolCallPart
from agentkit.models.model import Model
from agentkit.runtime.project import Project
from agentkit.runtime.spec import RunResult

JUDGE_PROMPT_VERSION = "v1"
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"score": {"type": "number"}, "reason": {"type": "string"}},
    "required": ["score", "reason"],
    "additionalProperties": False,
}


class GradeResult(BaseModel):
    grader: str = ""
    passed: bool
    score: float | None = None
    reason: str = ""
    skipped: bool = False  # a skipped grade does not count for or against the case


@dataclass
class GradeContext:
    project: Project
    judge: Callable[[], Model]  # builds the judge model when a grader first needs it


Grader = Callable[[Case, RunResult, dict[str, Any], GradeContext], Awaitable[GradeResult]]


def output_text(result: RunResult) -> str:
    output = result.output
    if output is None:
        return ""
    return output if isinstance(output, str) else json.dumps(to_jsonable_python(output))


def output_data(result: RunResult) -> Any:
    """The output as plain data: structured output as is, text parsed as JSON when it is JSON."""
    output = to_jsonable_python(result.output)
    if isinstance(output, str):
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return output
    return output


def called_tools(result: RunResult) -> list[ToolCallPart]:
    return [call for m in result.messages if m.role == "assistant" for call in m.tool_calls]


def _verdict(passed: bool, reason: str) -> GradeResult:
    return GradeResult(passed=passed, reason="" if passed else reason)


async def exact(
    case: Case, result: RunResult, params: dict[str, Any], ctx: GradeContext
) -> GradeResult:
    text = output_text(result).strip()
    return _verdict(text == str(params["value"]).strip(), f"output was {text[:200]!r}")


async def contains(
    case: Case, result: RunResult, params: dict[str, Any], ctx: GradeContext
) -> GradeResult:
    text = output_text(result)
    missing = [value for value in params["values"] if str(value) not in text]
    return _verdict(not missing, f"output lacks {missing}: {text[:200]!r}")


async def regex(
    case: Case, result: RunResult, params: dict[str, Any], ctx: GradeContext
) -> GradeResult:
    text = output_text(result)
    found = re.search(params["pattern"], text) is not None
    return _verdict(found, f"output does not match /{params['pattern']}/: {text[:200]!r}")


async def schema(
    case: Case, result: RunResult, params: dict[str, Any], ctx: GradeContext
) -> GradeResult:
    name = params["name"]
    model = ctx.project.schemas.get(name)
    if model is None:
        known = ", ".join(sorted(ctx.project.schemas)) or "none"
        raise AgentKitError(
            f"Case '{case.id}' expects schema '{name}', but no Pydantic model of that name is "
            f"defined in the project's agents/ or tools/. Known: {known}."
        )
    try:
        model.model_validate(output_data(result))
    except ValidationError as exc:
        return _verdict(False, f"output is not a valid {name}: {exc.errors()[0]['msg']}")
    return GradeResult(passed=True)


async def fields(
    case: Case, result: RunResult, params: dict[str, Any], ctx: GradeContext
) -> GradeResult:
    data = output_data(result)
    if not isinstance(data, dict):
        return _verdict(False, "output is not structured, so it has no fields")
    wrong = {k: data.get(k) for k, v in params["fields"].items() if data.get(k) != v}
    return _verdict(not wrong, f"fields differ from expected: got {wrong}")


def _matches(call: ToolCallPart, wanted: str | dict[str, Any]) -> bool:
    if isinstance(wanted, str):
        return call.name == wanted
    arguments = wanted.get("arguments") or {}
    return call.name == wanted.get("name") and all(
        call.arguments.get(key) == value for key, value in arguments.items()
    )


async def tool_calls(
    case: Case, result: RunResult, params: dict[str, Any], ctx: GradeContext
) -> GradeResult:
    calls = called_tools(result)
    names = [call.name for call in calls]
    problems = []
    for wanted in params.get("must_include") or []:
        if not any(_matches(call, wanted) for call in calls):
            problems.append(f"missing call {wanted}")
    for name in params.get("must_not_include") or []:
        if name in names:
            problems.append(f"forbidden call {name}")
    remaining = iter(names)
    in_order = params.get("in_order") or []
    if not all(name in remaining for name in in_order):
        problems.append(f"calls not in order {in_order}")
    return _verdict(not problems, f"{'; '.join(problems)} (called: {names})")


async def trajectory(
    case: Case, result: RunResult, params: dict[str, Any], ctx: GradeContext
) -> GradeResult:
    names = [call.name for call in called_tools(result)]
    problems = []
    if "tool_sequence" in params and names != params["tool_sequence"]:
        problems.append(f"tool sequence was {names}, expected {params['tool_sequence']}")
    if "max_steps" in params and result.steps > params["max_steps"]:
        problems.append(f"took {result.steps} steps, more than {params['max_steps']}")
    return _verdict(not problems, "; ".join(problems))


async def no_error(
    case: Case, result: RunResult, params: dict[str, Any], ctx: GradeContext
) -> GradeResult:
    detail = f": {result.error.message}" if result.error else ""
    return _verdict(
        result.stop_reason == "completed", f"run ended with {result.stop_reason}{detail}"
    )


async def within_budget(
    case: Case, result: RunResult, params: dict[str, Any], ctx: GradeContext
) -> GradeResult:
    usage = result.usage
    used = {
        "max_steps": result.steps,
        "max_tokens": usage.input_tokens + usage.output_tokens,
        "max_cost_usd": usage.cost_usd or 0.0,
        "max_seconds": result.duration_seconds,
    }
    unknown = sorted(set(params) - set(used))
    if unknown:
        raise AgentKitError(f"within_budget does not know {unknown}. Use {', '.join(used)}.")
    over = [f"{k} {used[k]:g} > {v:g}" for k, v in params.items() if used[k] > v]
    if result.stop_reason == "budget_exceeded":
        over.append("the run hit its own budget")
    return _verdict(not over, "; ".join(over))


async def llm_judge(
    case: Case, result: RunResult, params: dict[str, Any], ctx: GradeContext
) -> GradeResult:
    judge = ctx.judge()
    if judge.name == "fake":
        return GradeResult(passed=True, skipped=True, reason="the judge model is the fake provider")
    prompt = (Path(__file__).parent / "prompts" / f"judge_{JUDGE_PROMPT_VERSION}.md").read_text()
    question = (
        f"Rubric: {params['rubric']}\n\nTask given to the agent:\n{case.input}\n\n"
        f"Agent output:\n{output_text(result) or '(no output)'}"
    )
    response = await judge.generate(
        ModelRequest(
            messages=[Message.from_text("system", prompt), Message.from_text("user", question)],
            response_schema=JUDGE_SCHEMA,
        )
    )
    try:
        verdict = json.loads(response.message.text)
        score, reason = float(verdict["score"]), str(verdict["reason"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return _verdict(False, f"the judge did not return a score: {response.message.text[:200]!r}")
    minimum = params.get("min_score", ctx.project.config.evals.judge_min_score)
    return GradeResult(passed=score >= minimum, score=score, reason=reason)


async def custom(
    case: Case, result: RunResult, params: dict[str, Any], ctx: GradeContext
) -> GradeResult:
    target = str(params["function"])
    module_name, _, function_name = target.partition(":")
    try:
        function = getattr(importlib.import_module(module_name), function_name)
    except (ImportError, AttributeError) as exc:
        raise AgentKitError(
            f"Custom grader '{target}' could not be loaded ({exc}). Use 'module:function', with "
            "the module importable from the project directory."
        ) from exc
    grade = function(case, result)
    if inspect.isawaitable(grade):
        grade = await grade
    if not isinstance(grade, GradeResult):
        raise AgentKitError(f"Custom grader '{target}' must return a GradeResult.")
    return grade


GRADERS: dict[str, Grader] = {
    "exact": exact,
    "contains": contains,
    "regex": regex,
    "schema": schema,
    "fields": fields,
    "tool_calls": tool_calls,
    "trajectory": trajectory,
    "no_error": no_error,
    "within_budget": within_budget,
    "llm_judge": llm_judge,
    "custom": custom,
}


async def grade(
    case: Case, result: RunResult, grader_type: str, params: dict[str, Any], ctx: GradeContext
) -> GradeResult:
    if grader_type not in GRADERS:
        raise AgentKitError(
            f"Case '{case.id}' uses the unknown grader '{grader_type}'. "
            f"Available: {', '.join(GRADERS)}."
        )
    try:
        outcome = await GRADERS[grader_type](case, result, params, ctx)
    except ModelError as exc:
        outcome = GradeResult(passed=False, reason=f"the grader's model call failed: {exc}")
    except KeyError as exc:
        raise AgentKitError(
            f"Grader '{grader_type}' in case '{case.id}' needs the parameter {exc}."
        ) from exc
    return outcome.model_copy(update={"grader": grader_type})
