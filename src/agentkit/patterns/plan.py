"""Planner and executor: one agent writes a plan, another carries it out step by step."""

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from agentkit.patterns.base import Pattern, failure, text_of
from agentkit.runtime.context import RunContext
from agentkit.runtime.project import Project
from agentkit.runtime.spec import RunResult


class Plan(BaseModel):
    """What the planner must return."""

    steps: list[str] = Field(min_length=1)


class PlanAndExecute(Pattern):
    """The planner turns the task into an ordered list of steps. The executor then does
    each step in its own run, seeing the task and the results so far. The last step's
    output is the answer, so the planner should end the plan with the step that writes it.

    Use this when a task needs several steps that cannot be listed before seeing it. If
    you can list them in advance, use a Workflow."""

    kind: ClassVar[str] = "plan_and_execute"
    planner: str
    executor: str
    max_plan_steps: int = Field(default=8, ge=1)  # a longer plan is refused

    async def run(self, input: str, adapter: Any, project: Project, ctx: RunContext) -> RunResult:
        planner, executor = self.agents(project, self.planner, self.executor)
        planned: RunResult = await adapter.run(
            planner.model_copy(update={"output_schema": Plan}), input, ctx
        )
        if planned.stop_reason != "completed":
            return planned
        steps = planned.output.steps
        if len(steps) > self.max_plan_steps:
            return failure(
                planned,
                "plan_too_long",
                f"The plan has {len(steps)} steps; '{self.name}' allows {self.max_plan_steps}. "
                "Ask for a smaller task, tighten the planner's prompt, or raise max_plan_steps.",
            )
        finished: list[str] = []
        result = planned
        for number, step in enumerate(steps, 1):
            so_far = "".join(f"Step {i} result:\n{text}\n\n" for i, text in enumerate(finished, 1))
            task = (
                f"Overall task:\n{input}\n\n{so_far}"
                f"Your step now ({number} of {len(steps)}):\n{step}"
            )
            result = await adapter.run(executor, task, ctx)
            if result.stop_reason != "completed":
                break
            finished.append(text_of(result))
        return result
