"""`agentkit new`: copy the project template and fill in the names."""

import re
import shutil
from pathlib import Path

from agentkit.errors import AgentKitError

_NAME = re.compile(r"[a-z][a-z0-9_-]*")
PATTERNS = ("single", "router", "workflow")


def template_dir() -> Path:
    here = Path(__file__).resolve()
    # Installed wheel first, then a source checkout.
    for candidate in (here.parents[1] / "_templates", here.parents[3] / "templates"):
        if (candidate / "agent").is_dir():
            return candidate
    raise AgentKitError("The project template was not found. Reinstall agentkit.")


def create_project(name: str, parent: Path, pattern: str = "single") -> Path:
    """Create `parent/name` from the template. Returns the new project directory."""
    if not _NAME.fullmatch(name):
        raise AgentKitError(
            f"'{name}' is not a usable project name. Use lowercase letters, digits, '-' and "
            "'_', starting with a letter, such as 'support-agent'."
        )
    if pattern not in PATTERNS:
        raise AgentKitError(f"There is no '{pattern}' pattern. Available: {', '.join(PATTERNS)}.")
    target = parent / name
    if target.exists():
        raise AgentKitError(f"{target} already exists. Choose another name or remove it.")
    templates = template_dir()
    skip = shutil.ignore_patterns("__pycache__")
    shutil.copytree(templates / "agent", target, ignore=skip)
    if pattern != "single":
        # A pattern replaces the single agent's definition, prompts and starter evals.
        shutil.rmtree(target / "prompts")
        shutil.copytree(templates / pattern, target, ignore=skip, dirs_exist_ok=True)
    agent_name = name.replace("-", "_")
    for file in target.rglob("*"):
        if file.is_file() and file.name != ".gitkeep":
            text = file.read_text(encoding="utf-8")
            text = text.replace("__AGENT_NAME__", agent_name).replace("__PROJECT_NAME__", name)
            file.write_text(text, encoding="utf-8")
    return target
