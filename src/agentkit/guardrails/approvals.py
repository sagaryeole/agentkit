"""The approval gate: who decides whether a side-effecting tool call may run."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from agentkit.config import Config
from agentkit.errors import AgentKitError, ConfigError
from agentkit.tools.base import ToolFailure


class ApprovalRequest(BaseModel):
    run_id: str
    tool_name: str
    arguments: dict[str, Any]
    side_effects: str
    reason: str | None = None  # the model's stated reason, if any


class ApprovalDecision(BaseModel):
    outcome: Literal["approve", "deny", "edit"]
    edited_arguments: dict[str, Any] | None = None
    note: str | None = None


class ApprovalPending(AgentKitError):
    """The channel has not decided yet. The run stops and can be resumed with a decision."""

    def __init__(self, request: ApprovalRequest) -> None:
        super().__init__(f"Approval for {request.tool_name} is pending.")
        self.request = request


class ApprovalChannel(Protocol):
    async def request(self, req: ApprovalRequest) -> ApprovalDecision | None:
        """Decide on a tool call. Returning None suspends the run until a decision arrives."""
        ...

    async def ask(self, question: str) -> str:
        """Put a question from the agent to the operator and return the answer."""
        ...


class AutoDeny:
    """For unattended runs and evals: every request is denied and nobody answers questions."""

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(outcome="deny", note="This run is unattended.")

    async def ask(self, question: str) -> str:
        raise ToolFailure(
            "permission", "No operator is available in this run. Continue without asking."
        )


class AutoApprove(AutoDeny):
    """For tests only: every request is approved. Refuses to exist when `env` is prod."""

    def __init__(self, env: str) -> None:
        if env == "prod":
            raise ConfigError(
                "The auto_approve approval channel cannot be used when env is 'prod'. "
                "Set guardrails.approval_channel to cli, callback or auto_deny."
            )

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(outcome="approve")


class CallbackApproval:
    """Delegates to user-supplied functions, for web or chat integrations."""

    def __init__(
        self,
        decide: Callable[[ApprovalRequest], Awaitable[ApprovalDecision | None]],
        ask: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self._decide = decide
        self._ask = ask

    async def request(self, req: ApprovalRequest) -> ApprovalDecision | None:
        return await self._decide(req)

    async def ask(self, question: str) -> str:
        if self._ask is None:
            return await AutoDeny().ask(question)
        return await self._ask(question)


class CliApproval:
    """Asks the person at the terminal."""

    def __init__(self, prompt: Callable[[str], str] = input) -> None:
        self._prompt = prompt

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        lines = [
            "",
            f"Approval needed: {req.tool_name} ({req.side_effects})",
            f"  arguments: {json.dumps(req.arguments)}",
        ]
        if req.reason:
            lines.append(f"  reason:    {req.reason}")
        lines.append("[a]pprove, [d]eny or [e]dit arguments? ")
        answer = (await asyncio.to_thread(self._prompt, "\n".join(lines))).strip().lower()
        if answer.startswith("a"):
            return ApprovalDecision(outcome="approve")
        if answer.startswith("e"):
            raw = await asyncio.to_thread(self._prompt, "New arguments as JSON: ")
            try:
                edited = json.loads(raw)
            except json.JSONDecodeError:
                edited = None
            if isinstance(edited, dict):
                return ApprovalDecision(outcome="edit", edited_arguments=edited)
            return ApprovalDecision(outcome="deny", note="The edited arguments were not valid.")
        note = await asyncio.to_thread(self._prompt, "Reason for the agent (optional): ")
        return ApprovalDecision(outcome="deny", note=note.strip() or None)

    async def ask(self, question: str) -> str:
        return await asyncio.to_thread(self._prompt, f"\nThe agent asks: {question}\n> ")


def make_channel(cfg: Config, callback: CallbackApproval | None = None) -> ApprovalChannel:
    """The channel named by `guardrails.approval_channel`."""
    name = cfg.guardrails.approval_channel
    if name == "cli":
        return CliApproval()
    if name == "auto_deny":
        return AutoDeny()
    if name == "auto_approve":
        return AutoApprove(cfg.env)
    if callback is None:
        raise ConfigError(
            "guardrails.approval_channel is 'callback', so a CallbackApproval must be passed "
            "in code. Pass one, or choose cli or auto_deny."
        )
    return callback
