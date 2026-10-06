"""Shared builders for tests that run agents on a scripted model."""

from pathlib import Path
from typing import Any

from agentkit.config import Config, load_config
from agentkit.config.schema import Retry
from agentkit.models import Message, ModelResponse, ToolCallPart, Usage
from agentkit.models.base import ContentPart, TextPart
from agentkit.models.model import Link, Model
from agentkit.models.providers.fake import FakeProvider, Scripted
from agentkit.runtime.adapters.native import NativeAdapter
from agentkit.tools import ToolRegistry
from agentkit.tools.base import Tool

RETRY = Retry(max_attempts=1, base_delay_seconds=0, max_delay_seconds=0)


def config(tmp_path: Path, **overrides: Any) -> Config:
    return load_config(tmp_path, environ={}, overrides=overrides)


def tool_calls(*calls: tuple[str, dict[str, Any]], text: str = "") -> ModelResponse:
    """A model turn that asks for the given tool calls."""
    content: list[ContentPart] = [TextPart(text=text)] if text else []
    content += [
        ToolCallPart(id=f"call_{i}", name=name, arguments=arguments)
        for i, (name, arguments) in enumerate(calls)
    ]
    return ModelResponse(
        message=Message(role="assistant", content=content),
        stop_reason="tool_use",
        usage=Usage(input_tokens=10, output_tokens=5),
        model_id="fake",
    )


def scripted_adapter(
    script: list[Scripted], *tools: Tool, root: Path | None = None
) -> tuple[NativeAdapter, FakeProvider]:
    """A native adapter whose model replays `script`, with `tools` registered."""
    provider = FakeProvider(script)
    model = Model([Link("fake/scripted", provider, timeout_seconds=5)], RETRY)
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return NativeAdapter(registry, root=root, model_factory=lambda *a, **k: model), provider
