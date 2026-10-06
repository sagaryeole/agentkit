"""Per-run limits on steps, tool calls, tokens, cost and wall time."""

import time
from collections.abc import Callable

from agentkit.config.schema import Limits
from agentkit.errors import AgentKitError
from agentkit.models.base import Usage


class BudgetExceeded(AgentKitError):
    """A run reached one of its limits. `limit` names the config key that was hit."""

    def __init__(self, limit: str, value: float, maximum: float) -> None:
        super().__init__(
            f"The run reached limits.{limit} ({value:g} of {maximum:g}). Raise the limit in "
            "the config or the AgentSpec, or give the agent a smaller task."
        )
        self.limit = limit


class Budget:
    """Counts what a run has used. `check` is called before every model call and tool call."""

    def __init__(self, limits: Limits, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.limits = limits
        self.steps = 0
        self.tool_calls = 0
        self.usage = Usage(input_tokens=0, output_tokens=0)
        self._clock = clock
        self._started = clock()

    @property
    def elapsed_seconds(self) -> float:
        return self._clock() - self._started

    def restore(self, steps: int, tool_calls: int, usage: Usage) -> None:
        """Carry the counters of a suspended run into the run that resumes it."""
        self.steps, self.tool_calls, self.usage = steps, tool_calls, usage

    def record_model(self, usage: Usage) -> None:
        self.steps += 1
        self.usage = self.usage.plus(usage)

    def record_tool(self) -> None:
        self.tool_calls += 1

    def check(self, *, tool_call: bool = False) -> None:
        """Raise BudgetExceeded if another model call (or tool call) would pass a limit."""
        limits = self.limits
        used = [
            ("max_wall_seconds", self.elapsed_seconds, limits.max_wall_seconds),
            ("max_input_tokens", self.usage.input_tokens, limits.max_input_tokens),
            ("max_output_tokens", self.usage.output_tokens, limits.max_output_tokens),
            ("max_cost_usd", self.usage.cost_usd or 0.0, limits.max_cost_usd),
        ]
        if tool_call:
            used.insert(0, ("max_tool_calls", self.tool_calls, limits.max_tool_calls))
        else:
            used.insert(0, ("max_steps", self.steps, limits.max_steps))
        for name, value, maximum in used:
            # A zero-cost run never trips a zero cost limit.
            if value >= maximum and (value > 0 or name in ("max_steps", "max_tool_calls")):
                raise BudgetExceeded(name, value, maximum)
