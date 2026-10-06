# agentkit

A reusable foundation for building AI agent projects: shared tools, an eval harness, a provider-independent model layer, configuration, guardrails and observability, plus a template for starting new agents.

The design and build order are in [AGENTIC_FRAMEWORK_SPEC.md](AGENTIC_FRAMEWORK_SPEC.md). Milestones M1 (skeleton and configuration) and M2 (model layer: Anthropic, local and fake providers) are implemented; later milestones are not yet.

## Development

```
uv sync
uv run agentkit config validate
uv run agentkit doctor
uv run pytest
```

## Running on a local model

With LM Studio or Ollama serving a model, no cloud key is needed:

```
uv run agentkit models list --local          # IDs the server offers
export MODEL_PROVIDER=local LOCAL_MODEL_ID=<one of those IDs>
uv run agentkit models test default
uv run python examples/single_agent/agent.py "What is 17 times 23?"
```

LM Studio is the default server. For Ollama, set `models.local.server: ollama` in `agentkit.yaml` (or `AGENTKIT_MODELS__LOCAL__SERVER=ollama`). Unset `MODEL_PROVIDER` to go back to the hosted models in `config/models.yaml`; install the Anthropic SDK with the `anthropic` extra.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
