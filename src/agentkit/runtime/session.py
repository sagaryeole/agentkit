"""Run an agent as one turn of a longer conversation."""

from collections.abc import AsyncIterator
from typing import Any

from agentkit.errors import AgentKitError
from agentkit.guardrails.checks import run_checks
from agentkit.memory.sessions import SessionStore, storable
from agentkit.models.base import Message
from agentkit.runtime.context import RunContext
from agentkit.runtime.spec import AgentSpec, ErrorInfo, RunEvent, RunResult


async def stream_in_session(
    adapter: Any, spec: AgentSpec, text: str, ctx: RunContext, sessions: SessionStore
) -> AsyncIterator[RunEvent]:
    """Run `spec` on the user's message with the session's history in front of it, then
    add the turn to the history. `ctx.session_id` names the session.

    A turn that does not complete is stored as the user's message and a short note, so
    a half-finished tool round never ends up in the history.
    """
    if ctx.session_id is None:
        raise AgentKitError("A session needs an ID. Create the RunContext with session_id set.")
    checked = await run_checks(ctx.checks.user_input, text, ctx)
    if not checked.ok:
        reason = f"The input was rejected by a check: {checked.reason}."
        rejected = RunResult(
            run_id=ctx.run_id,
            output=None,
            stop_reason="error",
            usage=ctx.budget.usage,
            steps=0,
            error=ErrorInfo(kind="check_failed", message=reason),
        )
        yield RunEvent(type="done", result=rejected)
        return
    history = sessions.load(ctx.session_id)
    user = Message.from_text("user", checked.text)
    result: RunResult | None = None
    async for event in adapter.stream(spec, [*history, user], ctx):
        result = event.result or result
        yield event
    assert result is not None
    if result.stop_reason == "completed":
        # The transcript is the system prompt, the history, then this turn.
        turn = result.messages[1 + len(history) :]
    else:
        note = f"[This request was not completed: {result.stop_reason}.]"
        turn = [user, Message.from_text("assistant", note)]
    sessions.append(ctx.session_id, storable(turn))


async def run_in_session(
    adapter: Any, spec: AgentSpec, text: str, ctx: RunContext, sessions: SessionStore
) -> RunResult:
    result: RunResult | None = None
    async for event in stream_in_session(adapter, spec, text, ctx, sessions):
        result = event.result or result
    assert result is not None
    return result
