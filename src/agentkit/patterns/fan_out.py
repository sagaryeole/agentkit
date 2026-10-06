"""Parallel fan-out and merge: several agents work on the task at once, one combines."""

import asyncio
import dataclasses
from typing import Any, ClassVar

from agentkit.patterns.base import Pattern, text_of
from agentkit.runtime.context import RunContext
from agentkit.runtime.project import Project
from agentkit.runtime.spec import AgentSpec, RunResult


class FanOut(Pattern):
    """Every worker gets the same input and runs at the same time. The merger then gets
    the task and each worker's result, and its answer is the answer of the run.

    Use this when the work splits into parts that do not depend on each other, such as
    looking at one question from several angles. If any worker does not complete, the
    run stops with that worker's result and the merger is not called."""

    kind: ClassVar[str] = "fan_out"
    workers: list[str]
    merger: str

    async def run(self, input: str, adapter: Any, project: Project, ctx: RunContext) -> RunResult:
        *workers, merger = self.agents(project, *self.workers, self.merger)

        async def work(spec: AgentSpec) -> RunResult:
            # Each parallel run nests its own spans; everything else is shared.
            branch = dataclasses.replace(ctx, tracer=ctx.tracer.child())
            result: RunResult = await adapter.run(spec, input, branch)
            return result

        results = await asyncio.gather(*(work(spec) for spec in workers))
        for result in results:
            if result.stop_reason != "completed":
                return result
        parts = "\n\n".join(
            f"Result from {spec.name}:\n{text_of(result)}"
            for spec, result in zip(workers, results, strict=True)
        )
        merged: RunResult = await adapter.run(merger, f"Task:\n{input}\n\n{parts}", ctx)
        return merged
