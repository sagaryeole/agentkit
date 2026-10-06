"""One adapter per agent library. Each routes tool calls through the kit's tool executor."""

from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from agentkit.config import Config
from agentkit.errors import AgentKitError
from agentkit.models.base import Message
from agentkit.models.model import Model
from agentkit.models.registry import get_model
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


def get_adapter(
    cfg: Config,
    registry: ToolRegistry,
    root: Path | None = None,
    *,
    agents: Mapping[str, AgentSpec] | None = None,
    model_factory: Callable[..., Model] | None = None,
) -> RuntimeAdapter:
    """The adapter named by `runtime.adapter`. `agents` are what `sub_agents` may name;
    `model_factory` replaces how models are built, which evals use."""
    name = cfg.runtime.adapter
    if name == "native":
        return NativeAdapter(
            registry, root=root, agents=agents, model_factory=model_factory or get_model
        )
    if name == "adk":
        try:
            from agentkit.runtime.adapters.adk import AdkAdapter
        except ImportError as exc:
            raise AgentKitError(
                "The adk adapter needs Google ADK. Install it with `uv add 'agentkit[adk]'`."
            ) from exc
        return AdkAdapter(registry, cfg, root=root, model_factory=model_factory)
    raise AgentKitError(f"runtime.adapter '{name}' is not shipped. Use 'native' or 'adk'.")
