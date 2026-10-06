"""What the multi-agent patterns share."""

import json
import time
from typing import Any, ClassVar

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from agentkit.errors import AgentKitError
from agentkit.models.base import Message
from agentkit.runtime.context import RunContext
from agentkit.runtime.project import Project
from agentkit.runtime.spec import AgentSpec, ErrorInfo, RunResult


class Pattern(BaseModel):
    """Several agents arranged in a fixed shape, run by name like a single agent.

    Every agent in a pattern runs as a nested run of the pattern's own run span and
    draws on the same budget.
    """

    agentkit_pattern: ClassVar[bool] = True  # how a project recognises a pattern
    kind: ClassVar[str] = "pattern"

    name: str
    description: str

    async def run(self, input: str, adapter: Any, project: Project, ctx: RunContext) -> RunResult:
        raise NotImplementedError

    def agents(self, project: Project, *names: str) -> list[AgentSpec]:
        """The named agents, or an error that says which are not defined."""
        missing = [name for name in names if name not in project.agents]
        if missing or not names:
            raise AgentKitError(
                f"'{self.name}' needs at least one agent, and each must be defined in the "
                f"project. Not defined: {', '.join(missing) or 'none'}."
            )
        return [project.agents[name] for name in names]


def text_of(result: RunResult) -> str:
    """A run's output as the text handed to the next agent."""
    output = to_jsonable_python(result.output)
    return output if isinstance(output, str) else json.dumps(output)


def combined(
    result: RunResult, ctx: RunContext, started: float, messages: list[Message]
) -> RunResult:
    """`result`, restated for the whole pattern: total usage, all transcripts, total time."""
    return result.model_copy(
        update={
            "usage": ctx.budget.usage,
            "steps": ctx.budget.steps,
            "tool_calls": ctx.budget.tool_calls,
            "duration_seconds": time.monotonic() - started,
            "messages": messages,
        }
    )


def failure(result: RunResult, kind: str, message: str) -> RunResult:
    return result.model_copy(
        update={"stop_reason": "error", "error": ErrorInfo(kind=kind, message=message)}
    )


async def run_named(
    project: Project, adapter: Any, name: str, input: str, ctx: RunContext
) -> RunResult:
    """Run whatever the project defines under `name`: a pattern or a single agent."""
    pattern = project.patterns.get(name)
    if isinstance(pattern, Pattern):
        started = time.monotonic()
        messages: list[Message] = []
        with ctx.tracer.span("run", pattern.name, pattern=pattern.kind) as span:
            result = await pattern.run(input, _Collecting(adapter, messages), project, ctx)
            span.set(
                stop_reason=result.stop_reason,
                steps=ctx.budget.steps,
                tool_calls=ctx.budget.tool_calls,
            )
            if result.error is not None:
                span.fail(f"{result.error.kind}: {result.error.message}")
        ctx.tracer.agent_name = pattern.name
        return combined(result, ctx, started, messages)
    outcome: RunResult = await adapter.run(project.agent(name), input, ctx)
    return outcome


class _Collecting:
    """Passes runs through to the adapter and keeps every transcript, in finishing order."""

    def __init__(self, adapter: Any, messages: list[Message]) -> None:
        self._adapter = adapter
        self._messages = messages

    async def run(self, spec: AgentSpec, input: str, ctx: RunContext) -> RunResult:
        result: RunResult = await self._adapter.run(spec, input, ctx)
        self._messages.extend(result.messages)
        return result
