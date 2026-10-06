"""Find the agents, tools and schemas a project defines in its `agents/` and `tools/` folders."""

import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from agentkit.config import Config
from agentkit.errors import AgentKitError
from agentkit.runtime.spec import AgentSpec
from agentkit.tools.base import Tool
from agentkit.tools.registry import ToolRegistry

PACKAGES = ("tools", "agents")
_loaded_root: Path | None = None  # the project whose modules are currently imported


@dataclass
class Project:
    root: Path
    config: Config
    registry: ToolRegistry
    agents: dict[str, AgentSpec] = field(default_factory=dict)
    workflows: dict[str, BaseModel] = field(default_factory=dict)  # patterns.workflow.Workflow
    schemas: dict[str, type[BaseModel]] = field(default_factory=dict)

    def require(self, name: str) -> None:
        """Raise unless `name` is something this project can run."""
        if name not in self.workflows:
            self.agent(name)

    def agent(self, name: str) -> AgentSpec:
        if name not in self.agents:
            raise AgentKitError(
                f"No agent named '{name}' in {self.root / 'agents'}. "
                f"Defined agents: {', '.join(sorted(self.agents)) or 'none'}."
            )
        return self.agents[name]


def load_project(root: Path, cfg: Config) -> Project:
    """Import `tools/*.py` then `agents/*.py` under `root` and collect what they define.

    Every `Tool` joins the enabled builtin tools in the registry, every `AgentSpec` is an
    agent, and every Pydantic model is available to evals by its class name.
    """
    root = root.resolve()
    project = Project(root=root, config=cfg, registry=ToolRegistry.from_config(cfg))
    # A project's modules are imported as `tools.x`, `agents.x` and whatever else it has
    # (custom graders, say). Forget the previous project's, which may share those names.
    global _loaded_root
    for name, module in list(sys.modules.items()):
        file = getattr(module, "__file__", None)
        stale = _loaded_root is not None and file and Path(file).is_relative_to(_loaded_root)
        if stale or name.split(".")[0] in PACKAGES:
            del sys.modules[name]
    if _loaded_root is not None:
        sys.path[:] = [p for p in sys.path if p != str(_loaded_root)]
    _loaded_root = root
    sys.path[:] = [p for p in sys.path if p != str(root)]
    sys.path.insert(0, str(root))
    importlib.invalidate_caches()

    registered = {id(tool) for tool in project.registry}
    for package in PACKAGES:
        for file in sorted((root / package).glob("*.py")):
            if file.stem.startswith("_"):
                continue
            try:
                module = importlib.import_module(f"{package}.{file.stem}")
            except Exception as exc:
                raise AgentKitError(
                    f"Could not import {file}: {type(exc).__name__}: {exc}. Fix the file."
                ) from exc
            for value in vars(module).values():
                if isinstance(value, Tool) and id(value) not in registered:
                    project.registry.register(value)
                    registered.add(id(value))
                elif isinstance(value, AgentSpec):
                    project.agents[value.name] = value
                elif type(value).__name__ == "Workflow" and isinstance(value, BaseModel):
                    project.workflows[str(getattr(value, "name", ""))] = value
                elif (
                    isinstance(value, type)
                    and issubclass(value, BaseModel)
                    and value.__module__ == module.__name__
                ):
                    project.schemas.setdefault(value.__name__, value)
    return project
