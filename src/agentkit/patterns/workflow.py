"""Fixed workflow: known steps run in a fixed order, each feeding the next."""

import json
import time
from typing import Any

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from agentkit.errors import AgentKitError
from agentkit.models.base import Message
from agentkit.runtime.context import RunContext
from agentkit.runtime.project import Project
from agentkit.runtime.spec import RunResult


class Workflow(BaseModel):
    """Agents run one after another. The first gets the input; each later one gets the
    previous agent's output. Use this instead of one free-roaming agent whenever the
    steps are known in advance."""

    name: str
    description: str
    steps: list[str]  # agent names, in order


async def run_workflow(
    workflow: Workflow, input: str, adapter: Any, project: Project, ctx: RunContext
) -> RunResult:
    """Run the steps in order under one run span and one shared budget. Stops at the
    first step that does not complete and returns that step's result."""
    started = time.monotonic()
    missing = [step for step in workflow.steps if step not in project.agents]
    if missing or not workflow.steps:
        raise AgentKitError(
            f"Workflow '{workflow.name}' needs at least one step, and every step must name an "
            f"agent. Not defined: {', '.join(missing) or 'none'}."
        )
    messages: list[Message] = []
    current = input
    result: RunResult | None = None
    with ctx.tracer.span("run", workflow.name, pattern="workflow") as span:
        for step in workflow.steps:
            result = await adapter.run(project.agents[step], current, ctx)
            messages.extend(result.messages)
            if result.stop_reason != "completed":
                span.fail(f"step '{step}' ended with {result.stop_reason}")
                break
            output = to_jsonable_python(result.output)
            current = output if isinstance(output, str) else json.dumps(output)
        assert result is not None
        span.set(stop_reason=result.stop_reason, steps=ctx.budget.steps)
    ctx.tracer.agent_name = workflow.name
    return result.model_copy(
        update={
            "usage": ctx.budget.usage,
            "steps": ctx.budget.steps,
            "tool_calls": ctx.budget.tool_calls,
            "duration_seconds": time.monotonic() - started,
            "messages": messages,
        }
    )


async def run_named(
    project: Project, adapter: Any, name: str, input: str, ctx: RunContext
) -> RunResult:
    """Run whatever the project defines under `name`: a workflow or a single agent."""
    workflow = project.workflows.get(name)
    if isinstance(workflow, Workflow):
        return await run_workflow(workflow, input, adapter, project, ctx)
    result: RunResult = await adapter.run(project.agent(name), input, ctx)
    return result
