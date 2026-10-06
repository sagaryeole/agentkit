"""Keep a conversation inside the model's context window.

The transcript itself is never changed. Before a model call the adapter asks for a
`view` of it; when the transcript nears the window, the view is a trimmed copy.
"""

import json
from collections.abc import Awaitable, Callable

from agentkit.config.schema import ContextSettings
from agentkit.models.base import (
    Message,
    ModelRequest,
    OpaquePart,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
    estimate_tokens,
)

REMOVED = "[An earlier tool result was removed to save space.]"
SUMMARY_PREFIX = "Summary of the conversation so far:\n"


def render_transcript(messages: list[Message]) -> str:
    """Messages as plain text, for the model that summarises them."""
    lines = []
    for message in messages:
        for part in message.content:
            if isinstance(part, TextPart):
                lines.append(f"{message.role}: {part.text}")
            elif isinstance(part, ToolCallPart):
                lines.append(f"assistant called {part.name} with {json.dumps(part.arguments)}")
            elif isinstance(part, ToolResultPart):
                lines.append(f"tool result: {part.content[:2000]}")
    return "\n".join(lines)


def _without_opaque(messages: list[Message]) -> list[Message]:
    # A provider's reasoning blocks are only valid in the exact history that produced
    # them. Once the history is edited they must not be replayed, so they are left out.
    result = []
    for message in messages:
        content = [part for part in message.content if not isinstance(part, OpaquePart)]
        if content:
            result.append(message.model_copy(update={"content": content}))
    return result


class ContextManager:
    def __init__(
        self,
        settings: ContextSettings,
        context_window: int,
        summarize: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self._settings = settings
        self._limit = int(settings.trigger_ratio * context_window)
        self._summarize = summarize
        self._summary: tuple[int, str] | None = None  # (messages covered, summary text)
        self.trimmed = False  # whether the last view differed from the transcript

    def _fits(self, messages: list[Message], tools: list[ToolSpec]) -> bool:
        return estimate_tokens(ModelRequest(messages=messages, tools=tools)) <= self._limit

    async def view(self, messages: list[Message], tools: list[ToolSpec]) -> list[Message]:
        """The messages to send: the transcript itself while it fits, else a trimmed copy."""
        self.trimmed = False
        if self._settings.strategy == "off" or self._fits(messages, tools):
            return messages
        self.trimmed = True
        view = _without_opaque(messages)
        if self._settings.strategy == "summarize" and self._summarize is not None:
            view = await self._summarized(view)
        return self._drop_tool_results(view, tools)

    def _drop_tool_results(self, view: list[Message], tools: list[ToolSpec]) -> list[Message]:
        """Blank the oldest tool results, one at a time, until the view fits. Tool calls
        keep their results' places, so the history stays well-formed."""
        view = list(view)
        protected = len(view) - self._settings.keep_recent_messages
        for index in range(max(protected, 0)):
            if self._fits(view, tools):
                break
            message = view[index]
            if message.role != "tool":
                continue
            content = [
                part.model_copy(update={"content": REMOVED})
                if isinstance(part, ToolResultPart) and len(part.content) > len(REMOVED)
                else part
                for part in message.content
            ]
            view[index] = message.model_copy(update={"content": content})
        return view

    async def _summarized(self, view: list[Message]) -> list[Message]:
        """Replace everything before a recent user message with a summary of it."""
        assert self._summarize is not None
        start = 1 if view and view[0].role == "system" else 0
        newest = len(view) - self._settings.keep_recent_messages
        # Cut at the start of a turn, never between a tool call and its result.
        cut = next((i for i in range(newest, start, -1) if view[i].role == "user"), None)
        if cut is None:
            return view
        if self._summary is None or self._summary[0] < cut:
            covered = self._summary[0] if self._summary else start
            earlier = f"{SUMMARY_PREFIX}{self._summary[1]}\n\n" if self._summary else ""
            text = await self._summarize(earlier + render_transcript(view[covered:cut]))
            self._summary = (cut, text)
        summary = Message.from_text("user", SUMMARY_PREFIX + self._summary[1])
        return [*view[:start], summary, *view[self._summary[0] :]]
