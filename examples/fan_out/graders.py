from agentkit.evals.dataset import Case
from agentkit.evals.graders import GradeResult
from agentkit.runtime.spec import RunResult


def advisor_saw_both_sides(case: Case, result: RunResult) -> GradeResult:
    """The merger's input must carry each worker's result under its name."""
    seen = [m.text for m in result.messages if m.role == "user" and m.text.startswith("Task:")]
    ok = bool(seen) and all(
        part in seen[-1]
        for part in ("Result from benefits:\nMore time to test.", "Result from risks:\nThe press")
    )
    return GradeResult(passed=ok, reason="the advisor did not receive both results")
