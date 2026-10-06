"""Prompts live in files, versioned with the code, with simple {{name}} substitution."""

import re
from pathlib import Path

from agentkit.errors import AgentKitError
from agentkit.runtime.spec import AgentSpec

_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render_instructions(spec: AgentSpec, root: Path) -> str:
    """The agent's instructions: read from a prompt file if `instructions` names one under
    `root`, then with every {{name}} replaced from `spec.variables`."""
    text = spec.instructions
    if "\n" not in text and text.endswith((".md", ".txt")):
        path = root / text
        if not path.is_file():
            raise AgentKitError(
                f"Agent '{spec.name}' names the prompt file '{text}', which does not exist under "
                f"{root}. Create it, or put the instructions in the AgentSpec directly."
            )
        text = path.read_text(encoding="utf-8")

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in spec.variables:
            raise AgentKitError(
                f"The prompt of agent '{spec.name}' uses {{{{{name}}}}} but AgentSpec.variables "
                f"has no '{name}'. Add it to variables or remove the placeholder."
            )
        return spec.variables[name]

    return _PLACEHOLDER.sub(substitute, text).strip()
