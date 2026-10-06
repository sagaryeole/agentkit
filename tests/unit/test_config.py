from pathlib import Path

import pytest

from agentkit.config import load_config
from agentkit.errors import ConfigError


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_shipped_defaults_are_valid(tmp_path: Path) -> None:
    cfg = load_config(tmp_path, environ={})
    assert cfg.env == "dev"
    assert cfg.limits.max_steps == 25
    assert cfg.models.roles["default"] in cfg.models.models


def test_each_source_overrides_the_one_before(tmp_path: Path) -> None:
    write(tmp_path / "agentkit.yaml", "limits: {max_steps: 2, max_tool_calls: 2}\n")
    write(
        tmp_path / "agentkit.staging.yaml",
        "limits: {max_steps: 3, max_tool_calls: 3, max_cost_usd: 3}\n",
    )
    environ = {
        "AGENTKIT_ENV": "staging",
        "AGENTKIT_LIMITS__MAX_STEPS": "4",
        "AGENTKIT_LIMITS__MAX_TOOL_CALLS": "4",
        "AGENTKIT_LIMITS__MAX_COST_USD": "4",
        "AGENTKIT_LIMITS__MAX_WALL_SECONDS": "4",
    }
    overrides = {"limits": {"max_steps": 5}}

    def limits(**kwargs: object) -> tuple[float, ...]:
        lim = load_config(tmp_path, **kwargs).limits  # type: ignore[arg-type]
        return (lim.max_steps, lim.max_tool_calls, lim.max_cost_usd, lim.max_wall_seconds)

    assert limits(environ={}) == (2, 2, 2.0, 300)
    assert limits(environ={"AGENTKIT_ENV": "staging"}) == (3, 3, 3, 300)
    assert limits(environ=environ) == (4, 4, 4, 4)
    assert limits(environ=environ, overrides=overrides) == (5, 4, 4, 4)


def test_env_argument_selects_env_file(tmp_path: Path) -> None:
    write(tmp_path / "agentkit.staging.yaml", "limits: {max_steps: 7}\n")
    cfg = load_config(tmp_path, env="staging", environ={"AGENTKIT_ENV": "dev"})
    assert cfg.env == "staging"
    assert cfg.limits.max_steps == 7


def test_env_var_values_keep_their_type(tmp_path: Path) -> None:
    environ = {
        "AGENTKIT_OBSERVABILITY__CAPTURE_CONTENT": "false",
        "AGENTKIT_TOOLS__ENABLED": "[calculator]",
    }
    cfg = load_config(tmp_path, environ=environ)
    assert cfg.observability.capture_content is False
    assert cfg.tools.enabled == ["calculator"]


@pytest.mark.parametrize(
    "text", ["surprise: 1\n", "limits: {max_stepz: 1}\n", "models: {extra_key: 1}\n"]
)
def test_unknown_key_fails(tmp_path: Path, text: str) -> None:
    write(tmp_path / "agentkit.yaml", text)
    with pytest.raises(ConfigError, match="Extra inputs are not permitted"):
        load_config(tmp_path, environ={})


def test_unknown_env_var_key_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="limits.nope"):
        load_config(tmp_path, environ={"AGENTKIT_LIMITS__NOPE": "1"})


def test_role_must_point_to_a_catalogued_model(tmp_path: Path) -> None:
    write(tmp_path / "agentkit.yaml", "models: {roles: {default: nobody/nothing}}\n")
    with pytest.raises(ConfigError, match="nobody/nothing"):
        load_config(tmp_path, environ={})


def test_auto_approve_is_refused_in_prod(tmp_path: Path) -> None:
    write(tmp_path / "agentkit.yaml", "guardrails: {approval_channel: auto_approve}\n")
    assert load_config(tmp_path, environ={}).guardrails.approval_channel == "auto_approve"
    with pytest.raises(ConfigError, match="not allowed in prod"):
        load_config(tmp_path, env="prod", environ={})


def test_invalid_yaml_is_a_config_error(tmp_path: Path) -> None:
    write(tmp_path / "agentkit.yaml", "limits: [unclosed\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(tmp_path, environ={})
