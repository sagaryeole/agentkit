# ADK adapter

agentkit can run an `AgentSpec` on [Google's Agent Development Kit](https://google.github.io/adk-docs/) instead of its own loop. The same agent, tools, evals and guardrails work on both. This page explains how to switch, what is translated, and what is different.

## When to use it

The native runtime is the default, and is the right choice for most projects: it is small, supports everything in the kit, and is what the evals and tests are written against. Use the ADK adapter when:

- you already build on ADK and want to keep its runner, or
- you need ADK's own agent object (`to_native`) to plug into something that expects it.

The design goal is that switching runtimes is one configuration line and changes no agent code.

## Switch to it

```bash
uv sync --extra adk
```

```yaml
# agentkit.yaml
runtime:
  adapter: adk
```

or for one command: `AGENTKIT_RUNTIME__ADAPTER=adk uv run agentkit run ...`. Everything else is unchanged: `agentkit run`, `agentkit eval run` and the Python API all choose the adapter from this setting. The shipped eval suites in `examples/` pass on both, and CI runs a suite on each.

## What is translated

| agentkit | ADK |
|---|---|
| `AgentSpec` | An `LlmAgent`, built for each run. |
| A kit tool | An ADK tool whose every call goes through the kit's [tool executor](concepts.md#tools-and-the-executor). |
| A model role | An ADK model object: see below. |
| The budget | Checked in ADK's before-model callback and by the executor. |
| Tracing and cost | Recorded in ADK's after-model callback and by the executor. |

Because tool calls go through the kit's executor, **approvals, argument validation, timeouts, truncation, untrusted-content handling, output checks, tool-call budgets and tracing all apply**, whichever runtime made the call.

`adapter.to_native(spec)` returns the `LlmAgent` the adapter would run, for developers who want ADK's own object:

```python
agent = adapter.to_native(project.agents["my_agent"])     # a google.adk LlmAgent
```

### How models are mapped

| Catalogue entry | ADK model |
|---|---|
| `google` | The model ID, handled by ADK's own Gemini support. |
| `anthropic` | LiteLLM, as `anthropic/<model_id>`. |
| `local`, `openai`, `openai_compat` | LiteLLM, as `openai/<model_id>` with the endpoint's `api_base` and key. |
| `fake`, and anything an eval injects (scripted replies, record, replay) | A small ADK model backed by the kit's own model layer, so the same evals run on both runtimes. |

A local model, for example, becomes LiteLLM's `openai/<id>` form pointing at LM Studio or Ollama. Before the first call, the kit checks the local server and model ID, so you get the same clear start-up errors as on the native runtime.

## Differences and gaps

The adapter documents its gaps in its module docstring. They are:

- **Input must be a string.** A message history cannot be passed in, so [sessions and `agentkit chat`](sessions.md) need the native runtime, and long histories are not trimmed.
- **Approvals cannot be resumed.** A run that needs an approval nobody has given ends with `awaiting_approval` and no `pending` state. A synchronous channel (`cli`, or a callback that answers at once) works.
- **`sub_agents` and hand-offs are not mapped.** An agent with `sub_agents` raises an error. Router and hand-off patterns need the native runtime. The workflow, planner/executor, fan-out and reviser loop patterns run on both, because they are composed of separate agent runs.
- **The kit's retry, timeout and fallback do not apply to model calls** that ADK makes itself (hosted and local models). Budgets, cost, tracing and every tool guardrail still do. The exception is a model reached through the kit's own model layer (the fake provider and evals).
- **A call to a tool the agent does not have fails the run**, instead of being reported to the model so it can recover.
- **Structured output** is requested by instructing the model to answer in the schema's JSON, then validating it, rather than by a provider-level schema feature.

## Verification status

The adapter has been run end to end against a local model through LiteLLM, and its eval and unit suites run on it. Its Gemini and Anthropic mappings are checked by a unit test that inspects the objects it builds, and have not made live calls.

## Dependencies

`uv sync --extra adk` installs `google-adk` and `litellm`. ADK pins the OpenTelemetry API to a range, and kit's own requirement on it is deliberately loose so a patched ADK resolves. The dependency audit in CI checks that the resolved set has no known vulnerabilities.
