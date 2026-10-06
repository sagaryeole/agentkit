from agentkit.evals.dataset import Case
from agentkit.evals.graders import GradeResult
from agentkit.runtime.spec import RunResult


def stopped_at_second_step(case: Case, result: RunResult) -> GradeResult:
    """The script has one reply, so the second step fails and the workflow must stop there."""
    stopped = result.stop_reason == "error" and result.steps == 1
    return GradeResult(
        passed=stopped, reason=f"ended with {result.stop_reason} after {result.steps}"
    )
