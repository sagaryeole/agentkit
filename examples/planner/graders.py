from agentkit.evals.dataset import Case
from agentkit.evals.graders import GradeResult
from agentkit.runtime.spec import RunResult


def refused_as_too_long(case: Case, result: RunResult) -> GradeResult:
    """A plan longer than max_plan_steps must stop the run before any step is executed."""
    refused = result.error is not None and result.error.kind == "plan_too_long"
    return GradeResult(passed=refused and result.steps == 1, reason=f"ended with {result.error}")
