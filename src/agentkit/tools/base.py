"""The tool contract: the `@tool` decorator and the types a tool call produces."""

import asyncio
import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, get_type_hints, overload

from pydantic import BaseModel, ConfigDict, Field, create_model

from agentkit.config.schema import SideEffects
from agentkit.errors import AgentKitError
from agentkit.models.base import ToolSpec

if TYPE_CHECKING:
    from agentkit.runtime.context import RunContext

ErrorKind = Literal["invalid_input", "not_found", "permission", "timeout", "transient", "internal"]

_NAME = re.compile(r"[a-z][a-z0-9_]*")
_ARG_LINE = re.compile(r" {4}(\w+)(?: \(.*?\))?:\s*(.*)")


class ToolError(BaseModel):
    """A failure reported to the model. The message says what the model can do about it."""

    kind: ErrorKind
    message: str
    retryable: bool = False


class ToolFailure(AgentKitError):
    """Raise inside a tool to fail with a specific kind. It never reaches the model as a raise."""

    def __init__(self, kind: ErrorKind, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.error = ToolError(kind=kind, message=message, retryable=retryable)


class ToolResult(BaseModel):
    """Outcome of one tool call. `content` is the text handed to the model."""

    tool_name: str
    output: Any = None
    error: ToolError | None = None
    content: str
    truncated: bool = False
    duration_seconds: float


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    fn: Callable[..., Any]
    input_model: type[BaseModel]
    side_effects: SideEffects
    idempotent: bool
    timeout_seconds: float | None
    tags: tuple[str, ...]
    untrusted_output: bool
    wants_ctx: bool

    @property
    def spec(self) -> ToolSpec:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return ToolSpec(name=self.name, description=self.description, input_schema=schema)

    async def call(self, args: BaseModel, ctx: "RunContext") -> Any:
        """Run the function with validated arguments. Sync functions run in a thread."""
        kwargs = {name: getattr(args, name) for name in type(args).model_fields}
        if self.wants_ctx:
            kwargs["ctx"] = ctx
        if inspect.iscoroutinefunction(self.fn):
            return await self.fn(**kwargs)
        return await asyncio.to_thread(self.fn, **kwargs)


def _parse_docstring(fn: Callable[..., Any]) -> tuple[str, dict[str, str]]:
    """Split a Google-style docstring into the description and per-argument descriptions."""
    text = inspect.cleandoc(fn.__doc__ or "")
    description, _, arg_block = text.partition("\nArgs:")
    arguments: dict[str, str] = {}
    current = None
    for line in arg_block.splitlines():
        if line.strip() and not line.startswith(" "):
            break  # the next section, such as "Returns:"
        match = _ARG_LINE.fullmatch(line)
        if match:
            current = match.group(1)
            arguments[current] = match.group(2)
        elif current and line.strip():
            arguments[current] += " " + line.strip()
    return description.strip(), arguments


def _build(
    fn: Callable[..., Any],
    name: str | None,
    side_effects: SideEffects,
    idempotent: bool,
    timeout_seconds: float | None,
    tags: tuple[str, ...],
    untrusted_output: bool,
) -> Tool:
    tool_name = name or fn.__name__
    if not _NAME.fullmatch(tool_name):
        raise AgentKitError(f"Tool name '{tool_name}' must be snake_case, such as 'read_file'.")
    description, arg_docs = _parse_docstring(fn)
    if not description:
        raise AgentKitError(
            f"Tool '{tool_name}' has no docstring. Write one for the model: what the tool does, "
            "when to use it, when not to, and what each argument means."
        )
    hints = get_type_hints(fn, include_extras=True)
    fields: dict[str, Any] = {}
    parameters = inspect.signature(fn).parameters
    for param in parameters.values():
        if param.name == "ctx":
            continue
        if param.name not in hints:
            raise AgentKitError(
                f"Argument '{param.name}' of tool '{tool_name}' needs a type hint; the input "
                "schema is derived from it."
            )
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[param.name] = (
            hints[param.name],
            Field(default, description=arg_docs.get(param.name)),
        )
    input_model = create_model(
        f"{tool_name}_input", __config__=ConfigDict(extra="forbid"), **fields
    )
    return Tool(
        name=tool_name,
        description=description,
        fn=fn,
        input_model=input_model,
        side_effects=side_effects,
        idempotent=idempotent,
        timeout_seconds=timeout_seconds,
        tags=tags,
        untrusted_output=untrusted_output,
        wants_ctx="ctx" in parameters,
    )


@overload
def tool(fn: Callable[..., Any], /) -> Tool: ...
@overload
def tool(
    *,
    name: str | None = None,
    side_effects: SideEffects = "write",
    idempotent: bool = False,
    timeout_seconds: float | None = None,
    tags: tuple[str, ...] | list[str] = (),
    untrusted_output: bool = False,
) -> Callable[[Callable[..., Any]], Tool]: ...
def tool(
    fn: Callable[..., Any] | None = None,
    /,
    *,
    name: str | None = None,
    side_effects: SideEffects = "write",
    idempotent: bool = False,
    timeout_seconds: float | None = None,
    tags: tuple[str, ...] | list[str] = (),
    untrusted_output: bool = False,
) -> Tool | Callable[[Callable[..., Any]], Tool]:
    """Turn a function into a Tool.

    The input schema comes from the type hints and the description from the
    docstring, which is written for the model. A `ctx` parameter receives the
    RunContext and is hidden from the model. `side_effects` defaults to "write",
    so a tool that does not declare itself is treated as risky. Set
    `untrusted_output` when the result contains content from outside the kit
    (files, web pages); it is then handed to the model in a delimited block.
    """

    def wrap(function: Callable[..., Any]) -> Tool:
        return _build(
            function, name, side_effects, idempotent, timeout_seconds, tuple(tags), untrusted_output
        )

    return wrap(fn) if fn is not None else wrap
