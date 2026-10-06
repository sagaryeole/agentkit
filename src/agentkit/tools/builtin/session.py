"""Tools that talk to the operator, keep notes for the session, and end the task."""

from typing import Any

from agentkit.runtime.context import RunContext
from agentkit.tools.base import ToolFailure, tool


def _owner(ctx: RunContext) -> str:
    return ctx.session_id or ctx.run_id


@tool(side_effects="none", tags=["human"])
async def ask_human(question: str, *, ctx: RunContext) -> str:
    """Ask the operator a question and wait for the answer.

    Use this when you cannot continue without information only a person has, or to
    confirm something risky. Do not use it for things you can find out with other tools.

    Args:
        question: One clear question, with the context needed to answer it.
    """
    return await ctx.approvals.ask(question)


@tool(side_effects="write", tags=["memory"])
def remember(key: str, value: str, *, ctx: RunContext) -> str:
    """Save a short note under a key for the rest of this session.

    Use this for facts you will need again later in the conversation.
    Saving under an existing key replaces the old note.

    Args:
        key: A short name for the note, such as "customer_name".
        value: The note.
    """
    ctx.state.set("session", _owner(ctx), key, value)
    return f"Saved '{key}'."


@tool(side_effects="none", idempotent=True, tags=["memory"])
def recall(key: str | None = None, *, ctx: RunContext) -> dict[str, Any]:
    """Read back notes saved with `remember` in this session.

    Use this before asking the user for something they may already have told you.

    Args:
        key: The note to read. Leave it out to get every saved note.
    """
    owner = _owner(ctx)
    if key is None:
        return {
            name: ctx.state.get("session", owner, name) for name in ctx.state.list("session", owner)
        }
    value = ctx.state.get("session", owner, key)
    if value is None:
        saved = ", ".join(ctx.state.list("session", owner)) or "none"
        raise ToolFailure("not_found", f"No note is saved under '{key}'. Saved keys: {saved}.")
    return {key: value}


@tool(side_effects="none", tags=["control"])
def finish(summary: str, result: dict[str, Any] | None = None) -> str:
    """Declare the task complete and hand back the result. This ends the run.

    Call this once, when everything the task asked for is done.
    Do not call it to ask a question or to report partial progress.

    Args:
        summary: One or two sentences on what was done.
        result: The structured result, when the task asks for one.
    """
    return "Task complete."
