"""The eval dataset format: one suite per YAML or JSON-lines file."""

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentkit.errors import AgentKitError


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class GraderSpec(BaseModel):
    """A grader by `type`; every other key is a parameter of that grader."""

    model_config = ConfigDict(extra="allow")
    type: str

    @property
    def params(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class ToolCallExpect(_Strict):
    must_include: list[str | dict[str, Any]] = []  # a name, or {name, arguments} to match
    must_not_include: list[str] = []
    in_order: list[str] = []  # these calls must appear in this order


class OutputExpect(_Strict):
    exact: str | None = None
    contains: list[str] = []
    regex: str | None = None
    schema_name: str | None = Field(default=None, alias="schema")
    fields: dict[str, Any] = {}


class Expect(_Strict):
    tool_calls: ToolCallExpect | None = None
    output: OutputExpect | None = None


class FakeToolCall(_Strict):
    name: str
    arguments: dict[str, Any] = {}


class FakeTurn(_Strict):
    """One scripted model reply: text, tool calls, or both."""

    text: str = ""
    tool_call: FakeToolCall | None = None
    tool_calls: list[FakeToolCall] = []


class Setup(_Strict):
    fixtures: list[str] = []  # files in fixtures/ that script tool results
    fake_model: list[FakeTurn] | None = None  # scripted model replies; no model is called
    approve_tools: list[str] = []  # side-effecting tools this case may run


class Case(_Strict):
    id: str
    input: str
    setup: Setup = Setup()
    expect: Expect | None = None
    graders: list[GraderSpec | str] = []
    tags: list[str] = []


class Defaults(_Strict):
    graders: list[GraderSpec | str] = []


class Suite(_Strict):
    suite: str
    agent: str
    defaults: Defaults = Defaults()
    cases: list[Case]
    path: Path | None = Field(default=None, exclude=True)

    def graders_for(self, case: Case) -> list[GraderSpec]:
        """Suite defaults, then what `expect` implies, then the case's own graders."""
        specs = [GraderSpec(type=g) if isinstance(g, str) else g for g in self.defaults.graders]
        expect = case.expect
        if expect and expect.tool_calls:
            specs.append(GraderSpec(type="tool_calls", **expect.tool_calls.model_dump()))
        if expect and expect.output:
            out = expect.output
            if out.exact is not None:
                specs.append(GraderSpec(type="exact", value=out.exact))
            if out.contains:
                specs.append(GraderSpec(type="contains", values=out.contains))
            if out.regex is not None:
                specs.append(GraderSpec(type="regex", pattern=out.regex))
            if out.schema_name is not None:
                specs.append(GraderSpec(type="schema", name=out.schema_name))
            if out.fields:
                specs.append(GraderSpec(type="fields", fields=out.fields))
        specs += [GraderSpec(type=g) if isinstance(g, str) else g for g in case.graders]
        return specs


def load_suite(path: Path) -> Suite:
    """Read one suite. In a .jsonl file the line with a `suite` key is the header and
    every other line is a case."""
    try:
        if path.suffix == ".jsonl":
            rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            data = next((dict(row) for row in rows if "suite" in row), {})
            data["cases"] = [row for row in rows if "suite" not in row]
        else:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        suite = Suite.model_validate(data)
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise AgentKitError(f"{path}: not valid YAML or JSON ({exc}). Fix the syntax.") from exc
    except ValidationError as exc:
        problems = "\n".join(
            f"  {'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise AgentKitError(
            f"{path}: not a valid eval suite:\n{problems}\nSee the dataset format in the spec."
        ) from exc
    ids = [case.id for case in suite.cases]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise AgentKitError(f"{path}: case ids must be unique; repeated: {', '.join(duplicates)}.")
    suite.path = path
    return suite


def load_suites(path: Path) -> list[Suite]:
    """A suite file, or every suite file directly inside a directory."""
    if path.is_file():
        return [load_suite(path)]
    files = sorted(p for p in path.glob("*") if p.suffix in (".yaml", ".yml", ".jsonl"))
    if not files:
        raise AgentKitError(f"No eval suites (*.yaml, *.jsonl) found in {path}.")
    return [load_suite(file) for file in files]
