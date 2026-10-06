# agentkit

A reusable foundation for building AI agent projects: shared tools, an eval harness, a provider-independent model layer, configuration, guardrails and observability, plus a template for starting new agents.

The design and build order are in [AGENTIC_FRAMEWORK_SPEC.md](AGENTIC_FRAMEWORK_SPEC.md). Milestones M1 to M8 are implemented: configuration, the model layer, tools, the native runtime with budgets and approvals, tracing, evals, project scaffolding, a Google ADK adapter, and the router, workflow, hand-off and human-in-the-loop patterns. The documentation (M9) is not written yet.

## Quick start

```
uv sync
uv run agentkit new my-agent
cd my-agent
uv run --project .. agentkit eval run evals/        # starter evals; no API key needed
uv run --project .. agentkit run my_agent "What is 17 times 23?"
uv run --project .. agentkit trace <run id>
```

The projects under `examples/` (`single_agent`, `builtin_tools`, `router`, `workflow`, `human_in_loop`) are complete, each with an eval suite. `agentkit new --pattern router` or `--pattern workflow` starts from those shapes.

To run an agent on Google ADK instead of the kit's own loop, install the `adk` extra and set `runtime.adapter: adk`.

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
