from pathlib import Path

from agentkit.config import load_config
from agentkit.evals.dataset import load_suites
from agentkit.evals.runner import run_suites
from agentkit.runtime.project import load_project

ROOT = Path(__file__).parents[1]


async def test_starter_evals_pass() -> None:
    project = load_project(ROOT, load_config(ROOT))
    run = await run_suites(load_suites(ROOT / "evals"), project)
    failing = [case.id for suite in run.suites for case in suite.cases if case.pass_rate < 1]
    assert not failing
