"""Summaries of an eval run, and the case-by-case comparison of two runs."""

from statistics import mean

from pydantic import BaseModel

from agentkit.evals.runner import Attempt, EvalRun, SuiteResult


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]


def _stat(values: list[float], unit: str = "", digits: int = 1) -> str:
    if not values:
        return "n/a"
    return f"mean {mean(values):.{digits}f}{unit}, p95 {_p95(values):.{digits}f}{unit}"


def pass_rate(attempts: list[Attempt]) -> float:
    """Percentage of attempts that passed."""
    return 100.0 * sum(a.passed for a in attempts) / len(attempts) if attempts else 0.0


def all_attempts(run: EvalRun) -> list[Attempt]:
    return [a for suite in run.suites for case in suite.cases for a in case.attempts]


def render_suite(suite: SuiteResult) -> str:
    attempts = [a for case in suite.cases for a in case.attempts]
    lines = [
        f"Suite {suite.suite} (agent: {suite.agent})",
        f"  pass rate: {pass_rate(attempts):.1f}% "
        f"({sum(a.passed for a in attempts)}/{len(attempts)} attempts, {len(suite.cases)} cases)",
    ]
    by_grader: dict[str, list[bool]] = {}
    for attempt in attempts:
        for grade in attempt.grades:
            if not grade.skipped:
                by_grader.setdefault(grade.grader, []).append(grade.passed)
    for grader, outcomes in by_grader.items():
        lines.append(f"    {grader:<14} {100.0 * sum(outcomes) / len(outcomes):.1f}%")
    skipped = sorted({g.grader for a in attempts for g in a.grades if g.skipped})
    if skipped:
        lines.append(f"    skipped: {', '.join(skipped)}")
    costs = [a.cost_usd for a in attempts if a.cost_usd is not None]
    lines += [
        f"  steps:   {_stat([float(a.steps) for a in attempts])}",
        f"  tokens:  {_stat([float(a.tokens) for a in attempts], digits=0)}",
        f"  cost:    {_stat(costs, digits=4)}",
        f"  latency: {_stat([a.duration_seconds for a in attempts], 's', 2)}",
    ]
    failing = [case for case in suite.cases if case.pass_rate < 1]
    if failing:
        lines.append("  failing cases:")
    for case in failing:
        passed = sum(a.passed for a in case.attempts)
        lines.append(f"    {case.id} ({passed}/{len(case.attempts)} passed)")
        failed = next(a for a in case.attempts if not a.passed)
        for grade in failed.grades:
            if not grade.passed:
                lines.append(f"      {grade.grader}: {grade.reason}")
        if failed.trace_path:
            lines.append(f"      trace: {failed.run_id}")
    return "\n".join(lines)


def render_report(run: EvalRun) -> str:
    header = f"Eval run {run.id}  (repeat {run.repeat}"
    header += ", live tools" if run.live else ", fake tools"
    header += f", cassette {run.cassette})" if run.cassette else ")"
    return "\n\n".join([header, *(render_suite(suite) for suite in run.suites)])


class Comparison(BaseModel):
    baseline_pass_rate: float
    candidate_pass_rate: float
    regressions: list[str] = []
    improvements: list[str] = []
    added: list[str] = []
    removed: list[str] = []

    @property
    def drop(self) -> float:
        """How many points the pass rate fell; negative when it rose."""
        return self.baseline_pass_rate - self.candidate_pass_rate


def compare(baseline: EvalRun, candidate: EvalRun) -> Comparison:
    def rates(run: EvalRun) -> dict[str, float]:
        return {f"{s.suite}/{c.id}": c.pass_rate for s in run.suites for c in s.cases}

    before, after = rates(baseline), rates(candidate)
    result = Comparison(
        baseline_pass_rate=pass_rate(all_attempts(baseline)),
        candidate_pass_rate=pass_rate(all_attempts(candidate)),
        added=sorted(set(after) - set(before)),
        removed=sorted(set(before) - set(after)),
    )
    for case in sorted(set(before) & set(after)):
        change = f"{case}: {before[case]:.0%} -> {after[case]:.0%}"
        if after[case] < before[case]:
            result.regressions.append(change)
        elif after[case] > before[case]:
            result.improvements.append(change)
    return result


def render_comparison(baseline: EvalRun, candidate: EvalRun, result: Comparison) -> str:
    lines = [
        f"Baseline  {baseline.id}: {result.baseline_pass_rate:.1f}%",
        f"Candidate {candidate.id}: {result.candidate_pass_rate:.1f}%  "
        f"({result.candidate_pass_rate - result.baseline_pass_rate:+.1f} points)",
    ]
    for title, items in (
        ("Regressions", result.regressions),
        ("Improvements", result.improvements),
        ("New cases", result.added),
        ("Removed cases", result.removed),
    ):
        if items:
            lines.append(f"{title}:")
            lines += [f"  {item}" for item in items]
    if not (result.regressions or result.improvements):
        lines.append("No case changed.")
    return "\n".join(lines)
