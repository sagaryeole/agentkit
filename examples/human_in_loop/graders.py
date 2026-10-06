from agentkit.evals.dataset import Case
from agentkit.evals.graders import GradeResult
from agentkit.models.base import ToolResultPart
from agentkit.runtime.spec import RunResult


def _tool_results(result: RunResult) -> list[ToolResultPart]:
    return [
        part
        for message in result.messages
        if message.role == "tool"
        for part in message.content
        if isinstance(part, ToolResultPart)
    ]


def write_was_approved(case: Case, result: RunResult) -> GradeResult:
    results = _tool_results(result)
    ok = bool(results) and not results[0].is_error
    return GradeResult(passed=ok, reason="the write did not go through")


def write_was_denied(case: Case, result: RunResult) -> GradeResult:
    """The model must have been told that the operator did not approve."""
    results = _tool_results(result)
    ok = bool(results) and results[0].is_error and "did not approve" in results[0].content
    return GradeResult(passed=ok, reason="the denial did not reach the model")
