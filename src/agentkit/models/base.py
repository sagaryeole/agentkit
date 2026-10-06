"""Common types for the model layer and the interface every provider implements."""

from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, Field

from agentkit.errors import UnsupportedCapability

Role = Literal["system", "user", "assistant", "tool"]


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ImagePart(BaseModel):
    """An image, given either as base64 `data` or as a `url`."""

    type: Literal["image"] = "image"
    media_type: str = "image/png"
    data: str | None = None
    url: str | None = None


class ToolCallPart(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    arguments: dict[str, Any]


class ToolResultPart(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    content: str
    is_error: bool = False


class OpaquePart(BaseModel):
    """Provider-specific content that must be sent back unchanged, such as a reasoning block.

    Only the provider that produced it reads it; every other provider skips it.
    """

    type: Literal["opaque"] = "opaque"
    provider: str
    data: dict[str, Any]


ContentPart = Annotated[
    TextPart | ImagePart | ToolCallPart | ToolResultPart | OpaquePart,
    Field(discriminator="type"),
]


class Message(BaseModel):
    role: Role
    content: list[ContentPart]

    @classmethod
    def from_text(cls, role: Role, text: str) -> "Message":
        return cls(role=role, content=[TextPart(text=text)])

    @property
    def text(self) -> str:
        return "".join(part.text for part in self.content if isinstance(part, TextPart))

    @property
    def tool_calls(self) -> list[ToolCallPart]:
        return [part for part in self.content if isinstance(part, ToolCallPart)]


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]  # JSON Schema


class ModelRequest(BaseModel):
    messages: list[Message]
    tools: list[ToolSpec] = []
    tool_choice: Literal["auto", "required", "none"] | str = "auto"
    max_output_tokens: int | None = None
    temperature: float | None = None
    response_schema: dict[str, Any] | None = None  # structured output
    stop: list[str] = []
    provider_options: dict[str, Any] = {}  # passed to the provider untouched


class Usage(BaseModel):
    """Token counts. `input_tokens` is the total; `cached_input_tokens` of them were cache reads."""

    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    cost_usd: float | None = None

    def plus(self, other: "Usage") -> "Usage":
        costs = [c for c in (self.cost_usd, other.cost_usd) if c is not None]
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cost_usd=sum(costs) if costs else None,
        )


StopReason = Literal["end", "tool_use", "max_tokens", "stop_sequence", "refusal", "error"]


class ModelResponse(BaseModel):
    message: Message
    stop_reason: StopReason
    usage: Usage
    model_id: str
    raw: Any = Field(default=None, exclude=True)  # untouched provider response


class StreamEvent(BaseModel):
    """A `text_delta` carries new text; the final `done` event carries the whole response."""

    type: Literal["text_delta", "done"]
    text: str = ""
    response: ModelResponse | None = None


class Capabilities(BaseModel):
    tools: bool = True
    parallel_tool_calls: bool = True
    forced_tool_choice: bool = True
    structured_output: bool = True
    vision: bool = True
    streaming: bool = True
    prompt_caching: bool = False
    context_window: int
    max_output_tokens: int


class ModelProvider(Protocol):
    name: str
    capabilities: Capabilities

    async def generate(self, req: ModelRequest) -> ModelResponse: ...
    def stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]: ...
    async def count_tokens(self, req: ModelRequest) -> int: ...


def estimate_tokens(req: ModelRequest) -> int:
    """Rough count (four characters per token) for providers with no counting endpoint."""
    chars = sum(len(m.model_dump_json()) for m in req.messages)
    chars += sum(len(t.model_dump_json()) for t in req.tools)
    return max(1, chars // 4)


def check_request(req: ModelRequest, caps: Capabilities, model_key: str) -> None:
    """Raise UnsupportedCapability if `req` needs something the model lacks.

    A missing structured-output capability is not an error: the kit falls back to
    prompting for JSON and validating it.
    """

    def unsupported(what: str, field: str) -> UnsupportedCapability:
        return UnsupportedCapability(
            f"Model '{model_key}' does not support {what}. Use a model that does, or set "
            f"capabilities.{field} for this model in the models config if that is wrong."
        )

    if req.tools and not caps.tools:
        raise unsupported("tool calling", "tools")
    forced = req.tool_choice not in ("auto", "none")
    if req.tools and forced and not caps.forced_tool_choice:
        raise unsupported("forcing a tool call; use tool_choice 'auto'", "forced_tool_choice")
    has_image = any(isinstance(p, ImagePart) for m in req.messages for p in m.content)
    if has_image and not caps.vision:
        raise unsupported("image input", "vision")
    if req.max_output_tokens is not None and req.max_output_tokens > caps.max_output_tokens:
        raise UnsupportedCapability(
            f"Model '{model_key}' produces at most {caps.max_output_tokens} output tokens; "
            f"{req.max_output_tokens} were requested. Lower max_output_tokens."
        )
