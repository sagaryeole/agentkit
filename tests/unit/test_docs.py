"""The documentation must describe what the code does. These tests fail when it stops.

They check coverage (every command, option, builtin tool, configuration key and environment
variable is documented) and structure (the index lists every page, every relative link and
anchor resolves). They do not check that prose is correct; that is for review.
"""

import re
import types
import typing
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from typer.main import get_command

from agentkit.cli.main import app
from agentkit.config.schema import Config
from agentkit.tools.builtin import BUILTIN_TOOLS

ROOT = Path(__file__).parents[2]
DOCS = ROOT / "docs"
PAGES = [
    ROOT / name
    for name in (
        "README.md",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "SECURITY.md",
    )
] + sorted(DOCS.rglob("*.md"))


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def without_code(text: str) -> str:
    """The text with fenced code blocks and inline code removed."""
    text = re.sub(r"^(```|~~~).*?^\1[^\n]*$", "", text, flags=re.MULTILINE | re.DOTALL)
    return re.sub(r"`[^`\n]*`", "``", text)


def anchors(path: Path) -> set[str]:
    """The anchors GitHub generates for a page's headings."""
    text = re.sub(r"^(```|~~~).*?^\1[^\n]*$", "", read(path), flags=re.MULTILINE | re.DOTALL)
    found: set[str] = set()
    counts: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"#{1,6}\s+(.*?)\s*#*\s*$", line)
        if not match:
            continue
        slug = re.sub(r"[^\w\s-]", "", match.group(1).replace("`", "").lower())
        slug = re.sub(r"\s", "-", slug.strip())
        seen = counts.get(slug, 0)
        counts[slug] = seen + 1
        found.add(slug if seen == 0 else f"{slug}-{seen}")
    return found


# links


def links(path: Path) -> Iterator[str]:
    for match in re.finditer(
        r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", without_code(read(path))
    ):
        yield match.group(1)


@pytest.mark.parametrize("page", PAGES, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_and_anchors_resolve(page: Path) -> None:
    broken = []
    for link in links(page):
        if re.match(r"[a-z][a-z0-9+.-]*:", link):  # http:, https:, mailto:
            continue
        target, _, anchor = link.partition("#")
        destination = (page.parent / target).resolve() if target else page
        if not destination.exists():
            broken.append(f"{link}: no such file")
        elif anchor and destination.suffix == ".md" and anchor not in anchors(destination):
            broken.append(f"{link}: no heading '{anchor}' in {destination.relative_to(ROOT)}")
    assert not broken, f"{page.relative_to(ROOT)} has broken links:\n" + "\n".join(broken)


def test_the_index_lists_every_documentation_page() -> None:
    index = read(DOCS / "index.md")
    missing = [
        str(page.relative_to(DOCS))
        for page in DOCS.rglob("*.md")
        if page.name != "index.md" and f"({page.relative_to(DOCS)})" not in index
    ]
    assert not missing, f"docs/index.md does not link to: {', '.join(missing)}"


def test_readme_links_to_the_documentation_index() -> None:
    assert "(docs/index.md)" in read(ROOT / "README.md")


# CLI


def commands() -> Iterator[tuple[str, Any]]:
    """Every leaf command of the CLI with its full name. The CLI framework's command
    classes are not plain click ones, so groups are recognised by having sub-commands."""
    root = get_command(app)

    def walk(group: Any, prefix: str) -> Iterator[tuple[str, Any]]:
        for name, command in group.commands.items():
            if hasattr(command, "commands"):
                yield from walk(command, f"{prefix} {name}")
            else:
                yield f"{prefix} {name}", command

    yield from walk(root, "agentkit")


def section(text: str, heading: str) -> str:
    """The part of a page under the heading that contains `heading`, up to the next heading
    of the same or a higher level."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("#") and heading in line)
    level = len(lines[start]) - len(lines[start].lstrip("#"))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i].lstrip("#")
        if lines[i].startswith("#") and len(lines[i]) - len(stripped) <= level:
            end = i
            break
    return "\n".join(lines[start:end])


GLOBAL_OPTIONS = {"--help", "--env", "--project-dir"}


@pytest.mark.parametrize(
    "name,command", list(commands()), ids=lambda v: v if isinstance(v, str) else ""
)
def test_every_command_and_option_is_in_the_cli_reference(name: str, command: Any) -> None:
    doc = read(DOCS / "reference" / "cli.md")
    heading = f"`{name}`"
    assert heading in doc, f"docs/reference/cli.md has no heading {heading}"
    body = section(doc, heading)
    missing = [
        option
        for param in command.params
        if param.param_type_name == "option"
        for option in param.opts
        if option.startswith("--")
        and option not in GLOBAL_OPTIONS
        and f"| `{option}` |" not in body
    ]
    assert not missing, f"{heading} in the CLI reference has no table row for {missing}"


def test_the_global_options_are_documented() -> None:
    doc = read(DOCS / "reference" / "cli.md")
    assert "`--project-dir" in doc and "`--env" in doc


# builtin tools


@pytest.mark.parametrize("name", sorted(BUILTIN_TOOLS))
def test_every_builtin_tool_is_in_the_reference(name: str) -> None:
    doc = read(DOCS / "reference" / "builtin-tools.md")
    assert f"### `{name}`" in doc, f"docs/reference/builtin-tools.md has no section for {name}"
    body = section(doc, f"`{name}`")
    for argument in BUILTIN_TOOLS[name].input_model.model_fields:
        assert f"`{argument}`" in body, f"{name}: argument '{argument}' is not documented"


# configuration


def nested_model(annotation: object) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        for part in typing.get_args(annotation):
            if isinstance(part, type) and issubclass(part, BaseModel):
                return part
    return None


def config_keys(model: type[BaseModel], prefix: str = "") -> Iterator[str]:
    """Dotted paths of every configuration key, stopping at maps whose keys the user chooses."""
    for name, field in model.model_fields.items():
        nested = nested_model(field.annotation)
        if nested is not None:
            yield from config_keys(nested, f"{prefix}{name}.")
        else:
            yield f"{prefix}{name}"


def test_every_configuration_key_is_in_the_reference() -> None:
    doc = read(DOCS / "configuration.md")
    keys = list(config_keys(Config))
    assert len(keys) > 40  # the walk found the whole schema
    missing = [key for key in keys if f"`{key}`" not in doc]
    assert not missing, f"docs/configuration.md does not document: {missing}"


def test_every_environment_variable_is_documented() -> None:
    doc = read(DOCS / "configuration.md")
    for example in (ROOT / ".env.example", ROOT / "templates" / "agent" / ".env.example"):
        names = re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", read(example), flags=re.MULTILINE)
        assert names
        missing = [name for name in names if name not in doc]
        assert not missing, f"docs/configuration.md does not mention {missing} from {example.name}"


# the shipped defaults named in the docs


def test_documented_defaults_match_the_shipped_configuration() -> None:
    """A few defaults are quoted in prose in several places; keep them true."""
    import yaml

    shipped = yaml.safe_load(read(ROOT / "config" / "default.yaml"))
    doc = read(DOCS / "configuration.md")
    for section_name, key in (
        ("limits", "max_steps"),
        ("limits", "max_tool_calls"),
        ("limits", "max_input_tokens"),
        ("limits", "max_output_tokens"),
        ("limits", "max_wall_seconds"),
        ("retry", "max_attempts"),
        ("timeouts", "model_call_seconds"),
        ("timeouts", "tool_call_seconds"),
        ("evals", "concurrency"),
        ("evals", "judge_min_score"),
        ("guardrails", "max_identical_tool_calls"),
        ("guardrails", "max_empty_steps"),
    ):
        value = shipped[section_name][key]
        row = next(line for line in doc.splitlines() if f"`{section_name}.{key}`" in line)
        assert f"`{value}`" in row, (
            f"{section_name}.{key}: the docs do not say the default is {value}"
        )
