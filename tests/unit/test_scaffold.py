import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentkit.cli.main import app
from agentkit.cli.scaffold import create_project
from agentkit.errors import AgentKitError

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MODEL_PROVIDER", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "AGENTKIT_ENV"):
        monkeypatch.delenv(name, raising=False)


def test_new_project_passes_its_starter_evals_immediately(tmp_path: Path) -> None:
    created = runner.invoke(app, ["new", "support-agent", "--dir", str(tmp_path)])
    assert created.exit_code == 0, created.output
    root = tmp_path / "support-agent"
    expected = [
        "agentkit.yaml", ".env.example", ".gitignore", "pyproject.toml", "README.md",
        "agents/main.py", "prompts/main.md", "tools/text.py", "evals/smoke.yaml",
        "evals/fixtures", "tests/test_smoke.py",
    ]  # fmt: skip
    assert all((root / path).exists() for path in expected)
    assert "__AGENT_NAME__" not in (root / "agents" / "main.py").read_text()
    assert 'name="support_agent"' in (root / "agents" / "main.py").read_text()
    assert 'name = "support-agent"' in (root / "pyproject.toml").read_text()

    # no API key and no network: the starter evals script the model
    evals = runner.invoke(app, ["eval", "run", "--project-dir", str(root)])
    assert evals.exit_code == 0, evals.output
    assert "pass rate: 100.0% (3/3 attempts, 3 cases)" in evals.output

    assert runner.invoke(app, ["config", "validate", "--project-dir", str(root)]).exit_code == 0
    listed = runner.invoke(app, ["tools", "list", "--project-dir", str(root)])
    assert "calculator" in listed.output


def test_the_generated_project_tests_pass(tmp_path: Path) -> None:
    root = create_project("demo", tmp_path)
    env = {k: v for k, v in os.environ.items() if k not in ("MODEL_PROVIDER", "ANTHROPIC_API_KEY")}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_new_refuses_bad_names_existing_directories_and_unshipped_patterns(
    tmp_path: Path,
) -> None:
    with pytest.raises(AgentKitError, match="not a usable project name"):
        create_project("My Agent", tmp_path)
    create_project("demo", tmp_path)
    with pytest.raises(AgentKitError, match="already exists"):
        create_project("demo", tmp_path)
    with pytest.raises(AgentKitError, match="There is no 'swarm' pattern"):
        create_project("other", tmp_path, "swarm")
    failed = runner.invoke(app, ["new", "demo", "--dir", str(tmp_path)])
    assert failed.exit_code == 1 and "already exists" in failed.output


@pytest.mark.parametrize("pattern", ["router", "workflow"])
def test_pattern_projects_pass_their_starter_evals(tmp_path: Path, pattern: str) -> None:
    created = runner.invoke(app, ["new", "desk", "--pattern", pattern, "--dir", str(tmp_path)])
    assert created.exit_code == 0, created.output
    root = tmp_path / "desk"
    assert not (root / "prompts" / "main.md").exists()  # replaced by the pattern's prompts
    assert "__AGENT_NAME__" not in (root / "agents" / "main.py").read_text()
    evals = runner.invoke(app, ["eval", "run", "--project-dir", str(root)])
    assert evals.exit_code == 0, evals.output
    assert "pass rate: 100.0% (3/3 attempts, 3 cases)" in evals.output
