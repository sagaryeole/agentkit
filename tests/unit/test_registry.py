from pathlib import Path

import pytest

from agentkit.config import Config, load_config
from agentkit.errors import ModelError
from agentkit.models.providers.fake import FakeProvider
from agentkit.models.providers.local import LocalProvider
from agentkit.models.registry import get_model, resolve_chain


def config(tmp_path: Path, **overrides: object) -> Config:
    return load_config(tmp_path, environ={}, overrides=overrides)


def test_a_role_resolves_to_its_model_and_fallbacks(tmp_path: Path) -> None:
    cfg = config(tmp_path, models={"fallbacks": {"default": ["fake/default"]}})
    assert resolve_chain(cfg, "default", environ={}) == [
        "anthropic/claude-sonnet-5-5",
        "fake/default",
    ]
    assert resolve_chain(cfg, "fake/default", environ={}) == ["fake/default"]


def test_unknown_role_lists_the_roles(tmp_path: Path) -> None:
    with pytest.raises(ModelError, match="neither a model role .*default, fast"):
        resolve_chain(config(tmp_path), "nonsense", environ={})


def test_switching_the_role_to_another_provider_needs_only_config(tmp_path: Path) -> None:
    fake = get_model("default", config(tmp_path, models={"roles": {"default": "fake/default"}}))
    assert fake.name == "fake"
    assert fake.chain[0].price is not None and fake.chain[0].price.input == 0

    hosted = get_model("default", config(tmp_path), environ={})
    assert hosted.name == "anthropic"
    assert hosted.capabilities.context_window == 1_000_000  # from the catalogue
    assert hosted.capabilities.forced_tool_choice is False
    assert hosted.chain[0].defaults == {"max_output_tokens": 16000}
    assert hosted.chain[0].price is not None and hosted.chain[0].price.output == 10


def test_global_switch_moves_hosted_roles_to_local(tmp_path: Path) -> None:
    by_config = config(tmp_path, models={"provider": "local"})
    assert resolve_chain(by_config, "reasoning", environ={}) == ["local/default"]

    by_env = {"MODEL_PROVIDER": "local"}
    assert resolve_chain(config(tmp_path), "fast", environ=by_env) == ["local/default"]


def test_per_agent_variable_overrides_the_global_switch_both_ways(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    only_triage = {"TRIAGE_AGENT_MODEL_PROVIDER": "local"}
    assert resolve_chain(cfg, "default", agent_name="triage-agent", environ=only_triage) == [
        "local/default"
    ]
    assert resolve_chain(cfg, "default", agent_name="other", environ=only_triage) == [
        "anthropic/claude-sonnet-5-5"
    ]

    all_but_triage = {"MODEL_PROVIDER": "local", "TRIAGE_AGENT_MODEL_PROVIDER": "anthropic"}
    assert resolve_chain(cfg, "default", agent_name="triage-agent", environ=all_but_triage) == [
        "anthropic/claude-sonnet-5-5"
    ]


def test_local_never_falls_back_to_hosted_unless_configured_on_a_local_role(
    tmp_path: Path,
) -> None:
    hosted_chain = {"fallbacks": {"default": ["anthropic/claude-haiku-4-5"]}}
    switched = config(tmp_path, models=hosted_chain)
    assert resolve_chain(switched, "default", environ={"MODEL_PROVIDER": "local"}) == [
        "local/default"
    ]

    explicit = config(tmp_path, models={**hosted_chain, "roles": {"default": "local/default"}})
    assert resolve_chain(explicit, "default", environ={}) == [
        "local/default",
        "anthropic/claude-haiku-4-5",
    ]


def test_local_provider_is_built_from_config_and_environment(tmp_path: Path) -> None:
    cfg = config(tmp_path, models={"local": {"server": "ollama"}})
    model = get_model("default", cfg, environ={"MODEL_PROVIDER": "local", "LOCAL_MODEL_ID": "q"})
    provider = model.chain[0].provider
    assert isinstance(provider, LocalProvider)
    assert provider.api_base == "http://127.0.0.1:11434/v1"
    assert (provider.model_id, provider.server, provider.api_key) == ("q", "ollama", "local")
    assert provider.capabilities.structured_output is False
    assert model.chain[0].timeout_seconds == 300
    assert model.chain[0].price is not None and model.chain[0].price.output == 0

    environ = {"MODEL_PROVIDER": "local", "LOCAL_API_BASE": "http://box:9000/v1"}
    provider = get_model("default", cfg, environ=environ).chain[0].provider
    assert isinstance(provider, LocalProvider) and provider.api_base == "http://box:9000/v1"


def test_custom_server_needs_a_base_url(tmp_path: Path) -> None:
    cfg = config(tmp_path, models={"local": {"server": "custom"}})
    with pytest.raises(ModelError, match="Set LOCAL_API_BASE"):
        get_model("default", cfg, environ={"MODEL_PROVIDER": "local"})


def test_unshipped_provider_is_a_clear_error(tmp_path: Path) -> None:
    entry = {"provider": "openai", "model_id": "x"}
    cfg = config(tmp_path, models={"models": {"openai/x": entry}})
    with pytest.raises(ModelError, match="Available providers: anthropic, local, fake"):
        get_model("openai/x", cfg, environ={})


def test_fake_provider_is_built_for_the_fake_entry(tmp_path: Path) -> None:
    model = get_model("fake/default", config(tmp_path), environ={})
    assert isinstance(model.chain[0].provider, FakeProvider)
