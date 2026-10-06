from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from agentkit.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTKIT_ENV", raising=False)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "MODEL_PROVIDER", "LOCAL_MODEL_ID"):
        monkeypatch.delenv(name, raising=False)


def test_config_validate_ok(tmp_path: Path) -> None:
    result = runner.invoke(app, ["config", "validate", "--project-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "valid" in result.output


def test_config_validate_reports_unknown_key(tmp_path: Path) -> None:
    (tmp_path / "agentkit.yaml").write_text("limits: {max_stepz: 1}\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "validate", "--project-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "limits.max_stepz" in result.output


def test_config_show_prints_merged_yaml(tmp_path: Path) -> None:
    (tmp_path / "agentkit.yaml").write_text("limits: {max_steps: 9}\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "show", "--project-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert yaml.safe_load(result.output)["limits"]["max_steps"] == 9


def test_doctor_warns_about_missing_key(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor", "--project-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "[warn] keys: none of ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN is set" in result.output


def test_doctor_fails_on_invalid_config(tmp_path: Path) -> None:
    (tmp_path / "agentkit.yaml").write_text("surprise: 1\n", encoding="utf-8")
    result = runner.invoke(app, ["doctor", "--project-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "[fail] config" in result.output


def test_models_list_shows_roles(tmp_path: Path) -> None:
    result = runner.invoke(app, ["models", "list", "--project-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "default" in result.output and "[anthropic]" in result.output


def test_models_list_follows_the_local_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "local")
    result = runner.invoke(app, ["models", "list", "--project-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "local/default  [local]" in result.output
    assert "anthropic" not in result.output


def test_models_test_runs_the_fake_model(tmp_path: Path) -> None:
    result = runner.invoke(app, ["models", "test", "fake/default", "--project-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "reply:   fake: Reply with the single word: ok" in result.output
    assert "cost:    $0.000000" in result.output


def test_tools_list_and_describe(tmp_path: Path) -> None:
    listed = runner.invoke(app, ["tools", "list", "--project-dir", str(tmp_path)])
    assert listed.exit_code == 0
    assert "http_fetch     read_external" in listed.output

    described = runner.invoke(
        app, ["tools", "describe", "calculator", "--project-dir", str(tmp_path)]
    )
    assert described.exit_code == 0
    assert "side effects: none" in described.output and '"expression"' in described.output

    disabled = runner.invoke(
        app, ["tools", "describe", "list_files", "--project-dir", str(tmp_path)]
    )
    assert disabled.exit_code == 1 and "No tool named 'list_files'" in disabled.output


def test_tools_call_prints_what_the_model_would_see(tmp_path: Path) -> None:
    def call(*args: str) -> object:
        return runner.invoke(app, ["tools", "call", *args, "--project-dir", str(tmp_path)])

    ok = call("calculator", "--args", '{"expression": "6 * 7"}')
    assert (ok.exit_code, ok.output.strip()) == (0, "42")  # type: ignore[attr-defined]

    bad = call("calculator", "--args", '{"expression": "import os"}')
    assert bad.exit_code == 1 and '"kind": "invalid_input"' in bad.output  # type: ignore[attr-defined]

    not_json = call("calculator", "--args", "6 * 7")
    assert not_json.exit_code == 1 and "must be a JSON object" in not_json.output  # type: ignore[attr-defined]
