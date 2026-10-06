"""How an agent is described and what a run returns, independent of any agent library."""

from typing import Any, Literal

from pydantic import BaseModel

from agentkit.config.schema import Limits
from agentkit.guardrails.approvals import ApprovalRequest
from agentkit.models.base import Message, ToolCallPart, ToolResultPart, Usage
from agentkit.tools.base import ToolResult

StopReason = Literal[
    "completed", "budget_exceeded", "stalled", "denied", "error", "awaiting_approval"
]


class AgentSpec(BaseModel):
    name: str
    description: str
    # The prompt text, or the path of a prompt file relative to the project (prompts/main.md).
    instructions: str
    variables: dict[str, str] = {}  # values for {{name}} placeholders in the instructions
    model: str = "default"  # role or explicit key
    tools: list[str] = []
    sub_agents: list[str] = []
    output_schema: type[BaseModel] | None = None
    limits: Limits | None = None  # overrides the global limits


class ErrorInfo(BaseModel):
    kind: str
    message: str


class PendingApproval(BaseModel):
    """Everything needed to continue a run that stopped to wait for an approval.

    It is plain data: store it, and later pass it to the adapter's `resume` with the decision.
    """

    request: ApprovalRequest
    messages: list[Message]
    results: list[ToolResultPart]  # results of earlier tool calls in the same turn
    call_index: int  # which tool call of the last assistant message is waiting
    steps: int
    tool_calls: int
    usage: Usage


class RunResult(BaseModel):
    run_id: str
    output: Any
    stop_reason: StopReason
    usage: Usage  # totals for the run
    steps: int
    tool_calls: int = 0
    duration_seconds: float = 0.0
    trace_path: str | None = None
    error: ErrorInfo | None = None
    messages: list[Message] = []  # the full transcript
    pending: PendingApproval | None = None  # set when stop_reason is awaiting_approval


class RunEvent(BaseModel):
    """What `stream` yields: assistant text, each tool call and result, then `done`."""

    type: Literal["text", "tool_call", "tool_result", "done"]
    text: str = ""
    tool_call: ToolCallPart | None = None
    tool_result: ToolResult | None = None
    result: RunResult | None = None
