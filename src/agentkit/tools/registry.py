"""The set of tools available to a project, and the subset given to one agent."""

import warnings
from collections.abc import Iterable, Iterator

from agentkit.config import Config
from agentkit.errors import AgentKitError
from agentkit.tools.base import Tool


class ToolRegistry:
    def __init__(self, *, max_tools_per_agent: int = 20) -> None:
        self._tools: dict[str, Tool] = {}
        self._max_tools_per_agent = max_tools_per_agent

    @classmethod
    def from_config(cls, cfg: Config) -> "ToolRegistry":
        """A registry holding the builtin tools named in `tools.enabled`."""
        from agentkit.tools.builtin import BUILTIN_TOOLS

        registry = cls(max_tools_per_agent=cfg.tools.max_tools_per_agent)
        for name in cfg.tools.enabled:
            if name not in BUILTIN_TOOLS:
                raise AgentKitError(
                    f"tools.enabled lists '{name}', which is not a builtin tool. "
                    f"Builtin tools: {', '.join(sorted(BUILTIN_TOOLS))}. Remove it from the "
                    "list, or register your own tool in code."
                )
            registry.register(BUILTIN_TOOLS[name])
        return registry

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise AgentKitError(
                f"A tool named '{tool.name}' is already registered. Tool names must be unique; "
                "rename one of them."
            )
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise AgentKitError(
                f"No tool named '{name}' is registered. Registered: "
                f"{', '.join(sorted(self._tools)) or 'none'}."
            )
        return self._tools[name]

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def select(
        self, names: Iterable[str] | None = None, *, tags: Iterable[str] | None = None
    ) -> list[Tool]:
        """The tools for one agent: by name, by any of the tags, or all of them."""
        selected = list(self._tools.values()) if names is None else [self.get(n) for n in names]
        if tags is not None:
            wanted = set(tags)
            selected = [t for t in selected if wanted & set(t.tags)]
        if len(selected) > self._max_tools_per_agent:
            warnings.warn(
                f"{len(selected)} tools selected for one agent; tool choice gets less reliable "
                f"beyond {self._max_tools_per_agent}. Select fewer by name or tag.",
                stacklevel=2,
            )
        return selected
