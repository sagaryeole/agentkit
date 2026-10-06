"""One agent with one tool, written directly on the model layer.

The model comes from the `default` role, so the provider is chosen by
configuration alone:

    uv run python examples/single_agent/agent.py "What is 17 times 23?"
    MODEL_PROVIDER=local LOCAL_MODEL_ID=<id> uv run python examples/single_agent/agent.py "..."

This hand-written loop is replaced by an AgentSpec on the native runtime once
that exists.
"""

import asyncio
import sys

from agentkit.config import Config, load_config
from agentkit.models import Message, ModelRequest, ToolResultPart, ToolSpec, get_model

MULTIPLY = ToolSpec(
    name="multiply",
    description="Multiply two numbers and return the exact product. Use it for any multiplication.",
    input_schema={
        "type": "object",
        "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
        "required": ["a", "b"],
    },
)
INSTRUCTIONS = "You are a careful assistant. Use the multiply tool for multiplication."


async def run(question: str, cfg: Config | None = None) -> str:
    cfg = cfg or load_config()
    model = get_model("default", cfg, agent_name="single_agent")
    messages = [Message.from_text("system", INSTRUCTIONS), Message.from_text("user", question)]
    for _ in range(cfg.limits.max_steps):
        response = await model.generate(ModelRequest(messages=messages, tools=[MULTIPLY]))
        messages.append(response.message)
        calls = response.message.tool_calls
        if not calls:
            return response.message.text
        results = []
        for call in calls:
            try:
                content = str(call.arguments["a"] * call.arguments["b"])
                results.append(ToolResultPart(tool_call_id=call.id, content=content))
            except (KeyError, TypeError) as exc:
                problem = f"invalid arguments: {exc!r}"
                results.append(ToolResultPart(tool_call_id=call.id, content=problem, is_error=True))
        messages.append(Message(role="tool", content=list(results)))
    return "Stopped: the step limit was reached before an answer."


if __name__ == "__main__":
    print(asyncio.run(run(" ".join(sys.argv[1:]) or "What is 17 times 23?")))
