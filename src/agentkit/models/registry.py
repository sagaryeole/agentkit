"""Turn a role name into a ready-to-use Model, according to configuration."""

import os
import re
from collections.abc import Mapping

from agentkit.config import Config, load_config
from agentkit.config.schema import Price
from agentkit.errors import ModelError
from agentkit.models.base import Capabilities, ModelProvider
from agentkit.models.model import Link, Model

LOCAL_DEFAULT_KEY = "local/default"


def provider_switch(
    cfg: Config, agent_name: str | None = None, environ: Mapping[str, str] | None = None
) -> str | None:
    """The provider switch in force: per-agent variable, then MODEL_PROVIDER, then config."""
    environ = os.environ if environ is None else environ
    if agent_name:
        per_agent = environ.get(re.sub(r"\W", "_", agent_name).upper() + "_MODEL_PROVIDER")
        if per_agent:
            return per_agent
    return environ.get("MODEL_PROVIDER") or cfg.models.provider


def resolve_chain(
    cfg: Config,
    name: str,
    *,
    agent_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Model keys for a role or explicit key: the model to use, then its fallbacks."""
    catalogue = cfg.models.models
    key = cfg.models.roles.get(name, name)
    if key not in catalogue:
        raise ModelError(
            f"'{name}' is neither a model role ({', '.join(cfg.models.roles)}) nor a key in "
            "models.models. Use one of the roles, or add the model to the catalogue."
        )
    if provider_switch(cfg, agent_name, environ) == "local" and catalogue[key].provider != "local":
        if LOCAL_DEFAULT_KEY not in catalogue:
            raise ModelError(
                f"The provider switch is 'local' but models.models has no '{LOCAL_DEFAULT_KEY}' "
                "entry to move roles to. Add it back to the catalogue."
            )
        # Hosted fallbacks are dropped: going from local to hosted sends data off the
        # machine, so it must be configured on a role that is mapped to a local model.
        return [LOCAL_DEFAULT_KEY]
    return [key, *cfg.models.fallbacks.get(name, [])]


def local_api_base(cfg: Config, environ: Mapping[str, str] | None = None) -> str:
    from agentkit.models.providers.local import DEFAULT_API_BASES

    environ = os.environ if environ is None else environ
    local = cfg.models.local
    base = environ.get("LOCAL_API_BASE") or local.api_base or DEFAULT_API_BASES.get(local.server)
    if not base:
        raise ModelError(
            "models.local.server is 'custom', so the server address must be given. "
            "Set LOCAL_API_BASE or models.local.api_base, including the port and '/v1'."
        )
    return base


def _capabilities(defaults: Capabilities, cfg: Config, key: str) -> Capabilities:
    declared = cfg.models.models[key].capabilities
    if declared is None:
        return defaults
    return defaults.model_copy(update=declared.model_dump(exclude_none=True))


def build_provider(
    cfg: Config, key: str, environ: Mapping[str, str] | None = None
) -> ModelProvider:
    """Construct the provider for one catalogue entry. Provider modules are imported lazily."""
    environ = os.environ if environ is None else environ
    entry = cfg.models.models[key]
    if entry.provider == "fake":
        from agentkit.models.providers import fake

        return fake.FakeProvider(
            model_id=entry.model_id,
            capabilities=_capabilities(fake.DEFAULT_CAPABILITIES, cfg, key),
        )
    if entry.provider == "local":
        from agentkit.models.providers import local

        model_id = entry.model_id
        if key == LOCAL_DEFAULT_KEY:
            model_id = environ.get("LOCAL_MODEL_ID") or model_id
        return local.LocalProvider(
            model_id,
            api_base=local_api_base(cfg, environ),
            server=cfg.models.local.server,
            api_key=environ.get("LOCAL_API_KEY") or cfg.models.local.api_key,
            capabilities=_capabilities(local.DEFAULT_CAPABILITIES, cfg, key),
        )
    if entry.provider == "anthropic":
        try:
            from agentkit.models.providers import anthropic
        except ImportError as exc:
            raise ModelError(
                f"Model '{key}' needs the Anthropic SDK. Install it with "
                "`uv add 'agentkit[anthropic]'`."
            ) from exc
        return anthropic.AnthropicProvider(
            entry.model_id,
            capabilities=_capabilities(anthropic.DEFAULT_CAPABILITIES, cfg, key),
        )
    raise ModelError(
        f"Model '{key}' uses provider '{entry.provider}', which this version does not ship. "
        "Available providers: anthropic, local, fake."
    )


def _price(cfg: Config, key: str) -> Price | None:
    provider = cfg.models.models[key].provider
    return cfg.pricing.get(key) or cfg.pricing.get(f"{provider}/*")


def get_model(
    name: str = "default",
    cfg: Config | None = None,
    *,
    agent_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Model:
    """The model for a role ("default", "fast", ...) or, discouraged, an explicit model key.

    `agent_name` lets `<AGENT_NAME>_MODEL_PROVIDER` switch a single agent to or from local.
    """
    cfg = cfg or load_config()
    links = []
    for key in resolve_chain(cfg, name, agent_name=agent_name, environ=environ):
        entry = cfg.models.models[key]
        timeout = cfg.timeouts.model_call_seconds
        if entry.provider == "local":
            timeout = cfg.models.local.request_timeout_seconds
        links.append(
            Link(
                key=key,
                provider=build_provider(cfg, key, environ),
                timeout_seconds=timeout,
                price=_price(cfg, key),
                defaults=entry.defaults,
            )
        )
    return Model(links, cfg.retry)
