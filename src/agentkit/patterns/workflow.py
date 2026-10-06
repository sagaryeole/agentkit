"""Fixed workflow: known steps run in a fixed order, each feeding the next."""

from typing import Any, ClassVar

from agentkit.patterns.base import Pattern, text_of
from agentkit.runtime.context import RunContext
from agentkit.runtime.project import Project
from agentkit.runtime.spec import RunResult


class Workflow(Pattern):
    """Agents run one after another. The first gets the input; each later one gets the
    previous agent's output. Use this instead of one free-roaming agent whenever the
    steps are known in advance. It stops at the first step that does not complete."""

    kind: ClassVar[str] = "workflow"
    steps: list[str]  # agent names, in order

    async def run(self, input: str, adapter: Any, project: Project, ctx: RunContext) -> RunResult:
        current = input
        for spec in self.agents(project, *self.steps):
            result: RunResult = await adapter.run(spec, current, ctx)
            if result.stop_reason != "completed":
                break
            current = text_of(result)
        return result
