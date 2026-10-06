"""One adapter per agent library. Each routes tool calls through the kit's tool executor."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol

from agentkit.config import Config
from agentkit.errors import AgentKitError
from agentkit.models.base import Message
from agentkit.runtime.adapters.native import NativeAdapter
from agentkit.runtime.context import RunContext
from agentkit.runtime.spec import AgentSpec, RunEvent, RunResult
from agentkit.tools.registry import ToolRegistry


class RuntimeAdapter(Protocol):
    async def run(
        self, spec: AgentSpec, input: str | list[Message], ctx: RunContext
    ) -> RunResult: ...

    def stream(
        self, spec: AgentSpec, input: str | list[Message], ctx: RunContext
    ) -> AsyncIterator[RunEvent]: ...

    def to_native(self, spec: AgentSpec) -> Any:
        """The library's own agent object, for developers who need it."""
        ...


def get_adapter(cfg: Config, registry: ToolRegistry, root: Path | None = None) -> NativeAdapter:
    """The adapter named by `runtime.adapter`."""
    if cfg.runtime.adapter != "native":
        raise AgentKitError(
            f"runtime.adapter '{cfg.runtime.adapter}' is not shipped yet. Use 'native'."
        )
    return NativeAdapter(registry, root=root)
