"""Stand-ins used by evals: tools answered from fixtures, and an approval channel per case."""

import dataclasses
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import yaml
from pydantic_core import to_jsonable_python

from agentkit.errors import AgentKitError
from agentkit.guardrails.approvals import ApprovalDecision, ApprovalRequest, AutoDeny
from agentkit.tools.base import Tool, ToolFailure
from agentkit.tools.registry import ToolRegistry

Rules = list[dict[str, Any]]


def load_fixtures(directory: Path, names: Iterable[str]) -> dict[str, Rules]:
    """Merge the named fixture files into {tool name: rules}. Later files win per tool."""
    rules: dict[str, Rules] = {}
    for name in names:
        path = directory / f"{name}.yaml"
        if not path.is_file():
            raise AgentKitError(f"Fixture '{name}' not found at {path}. Create it or fix the name.")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        tools = data.get("tools")
        if not isinstance(tools, dict):
            raise AgentKitError(f"{path}: a fixture needs a top-level `tools:` mapping.")
        rules.update(tools)
    return rules


def _scripted(tool_name: str, rules: Rules) -> Callable[..., Any]:
    def fake(**arguments: Any) -> Any:
        given = to_jsonable_python(arguments)
        for rule in rules:
            when = rule.get("when") or {}
            if all(given.get(key) == value for key, value in when.items()):
                if "error" in rule:
                    error = rule["error"]
                    raise ToolFailure(error.get("kind", "internal"), error.get("message", ""))
                return rule.get("returns")
        raise ToolFailure(
            "not_found", f"The fixture for {tool_name} has no rule matching these arguments."
        )

    return fake


def _unavailable(tool_name: str) -> Callable[..., Any]:
    def fake(**arguments: Any) -> Any:
        raise ToolFailure(
            "not_found",
            f"{tool_name} has no fixture in this eval. Add one under fixtures/, or run the "
            "eval with --live to use the real tool.",
        )

    return fake


def eval_registry(
    registry: ToolRegistry, fixtures: dict[str, Rules], *, live: bool
) -> ToolRegistry:
    """The tools a case runs against. Unless `live`: a tool with a fixture answers from it,
    a tool without side effects runs for real, and any other tool reports a missing fixture."""
    unknown = sorted(set(fixtures) - {tool.name for tool in registry})
    if unknown:
        raise AgentKitError(f"Fixtures name tools that are not registered: {', '.join(unknown)}.")
    result = ToolRegistry()
    for tool in registry:
        replacement: Tool = tool
        if not live and tool.name in fixtures:
            fake = _scripted(tool.name, fixtures[tool.name])
            replacement = dataclasses.replace(tool, fn=fake, wants_ctx=False)
        elif not live and tool.side_effects != "none":
            replacement = dataclasses.replace(tool, fn=_unavailable(tool.name), wants_ctx=False)
        result.register(replacement)
    return result


class EvalApprovals(AutoDeny):
    """Denies every side-effecting call except the tools a case explicitly allows."""

    def __init__(self, approve_tools: Iterable[str]) -> None:
        self._approved = set(approve_tools)

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        if req.tool_name in self._approved:
            return ApprovalDecision(outcome="approve")
        return ApprovalDecision(outcome="deny", note="Not allowed in this eval case.")
