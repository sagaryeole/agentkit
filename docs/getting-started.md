# Getting started

This guide takes you from nothing to an agent with its own tool, an eval that checks it, and a real model behind it. Allow about ten minutes. Nothing before the "real model" step needs an API key.

## 1. Install

You need [uv](https://docs.astral.sh/uv/). It also installs Python 3.11 or newer if you do not have it.

```bash
git clone <repository-url> agentkit
cd agentkit
uv sync
uv run agentkit doctor
```

`doctor` checks the configuration and your credentials. With no credentials set you will see warnings, not failures (excerpt):

```
[ ok ] config: valid (env: dev)
[warn] keys: none of ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN is set; 'anthropic' models will fail unless the SDK finds a login profile
```

That is fine for now. The optional extras add provider SDKs: `uv sync --extra anthropic` for Claude, `--extra adk` for Google ADK, `--extra mcp` for MCP servers. You can install them later, when you need them.

> **Working in your own repository?** Add agentkit as a dependency instead of working inside the checkout: `uv add "agentkit[anthropic] @ git+<repository-url>"`. Then run `agentkit` (rather than `uv run agentkit` from the checkout) inside your project's environment. Everything below works the same way.

## 2. Create a project

```bash
uv run agentkit new my-agent
```

This creates `my-agent/`:

```
my-agent/
├── agentkit.yaml        # this project's configuration
├── agents/main.py       # the agent, described as an AgentSpec
├── prompts/main.md      # its prompt
├── tools/text.py        # a project tool, word_count
├── evals/smoke.yaml     # three starter eval cases
├── tests/test_smoke.py  # runs those evals under pytest
├── pyproject.toml
├── .env.example
└── README.md
```

Open `agents/main.py`. An agent is a description, not a program:

```python
from agentkit.runtime.spec import AgentSpec

agent = AgentSpec(
    name="my_agent",
    description="Answers questions, using tools for calculations and text statistics.",
    instructions="prompts/main.md",
    tools=["calculator", "word_count"],
)
```

`instructions` is the path of a prompt file, or the prompt text itself. `tools` lists the only tools this agent may use. `calculator` is a builtin tool; `word_count` is defined in `tools/text.py`. The agent uses the `default` model role unless you set `model=`.

## 3. Run the starter evals

```bash
uv run agentkit eval run --project-dir my-agent
```

```
Suite my_agent_smoke (agent: my_agent)
  pass rate: 100.0% (3/3 attempts, 3 cases)
```

These cases script the model's replies (see `evals/smoke.yaml`), so they pass with no network and no key. They do not test a model. They check everything around it: that the prompt file loads, that the tools exist and validate their arguments, that limits hold and that the output is read correctly. That makes them fast enough to run on every change.

## 4. Add a tool

Create `my-agent/tools/units.py`:

```python
from agentkit.tools import tool


@tool(side_effects="none", idempotent=True)
def km_to_miles(km: float) -> float:
    """Convert a distance from kilometres to miles.

    Use this for any kilometre-to-mile conversion instead of working it out yourself.

    Args:
        km: The distance in kilometres.
    """
    return round(km * 0.621371, 2)
```

Three things in that file matter. The **type hints** become the input schema. The **docstring** is what the model reads to decide when to call the tool, so it says what the tool does, when to use it and what each argument means. And **`side_effects`** declares whether the tool changes anything; a tool that leaves it out is treated as `write` and needs approval before it runs.

Give the agent the tool by adding its name in `my-agent/agents/main.py`:

```python
    tools=["calculator", "word_count", "km_to_miles"],
```

Look at what the model will be told about it, and try one call without running an agent:

```bash
uv run agentkit tools describe km_to_miles --project-dir my-agent
uv run agentkit tools call km_to_miles --args '{"km": 10}' --project-dir my-agent
```

The second command prints `6.21`, which is exactly the text the model would receive. Mistakes come back as errors the model can act on, for example `--args '{"km": "far"}'`.

## 5. Write an eval for it

Create `my-agent/evals/units.yaml`:

```yaml
suite: units
agent: my_agent
defaults:
  graders: [no_error]

cases:
  - id: converts_km_with_the_tool
    input: "How many miles is 10 km?"
    setup:
      fake_model:
        - tool_call: { name: km_to_miles, arguments: { km: 10 } }
        - text: "10 km is 6.21 miles."
    expect:
      tool_calls:
        must_include:
          - { name: km_to_miles, arguments: { km: 10 } }
      output:
        contains: ["6.21"]
```

```bash
uv run agentkit eval run my-agent/evals/units.yaml --project-dir my-agent
```

```
Suite units (agent: my_agent)
  pass rate: 100.0% (1/1 attempts, 1 cases)
```

The case describes an input, scripts the model's two replies (call the tool, then answer), and says what must be true: the tool was called with `km: 10`, and the answer contains `6.21`. See [Writing evals](writing-evals.md) for everything else a case can check.

## 6. Connect a real model

Pick one.

### A model on your own machine

Install [LM Studio](https://lmstudio.ai) or [Ollama](https://ollama.com), load a model that supports tool calling, and start its server. Then:

```bash
uv run agentkit models list --local                     # the IDs the server offers
export MODEL_PROVIDER=local LOCAL_MODEL_ID=<one of those IDs>
uv run agentkit models test default
uv run agentkit run my_agent "How many miles is 10 km?" --project-dir my-agent
```

LM Studio is the default server. For Ollama, set `models.local.server: ollama` in `my-agent/agentkit.yaml`, or export `AGENTKIT_MODELS__LOCAL__SERVER=ollama`. The base URL must include the port and `/v1`; the defaults are `http://127.0.0.1:1234/v1` for LM Studio and `http://127.0.0.1:11434/v1` for Ollama.

Local models vary a great deal. Some ignore tools, and small ones have small context windows. Declare what yours supports under `local/default` in the models config; see [Models and providers](models.md#local-models).

### Anthropic

```bash
uv sync --extra anthropic
export ANTHROPIC_API_KEY=...
uv run agentkit models test default
uv run agentkit run my_agent "How many miles is 10 km?" --project-dir my-agent
```

The roles map to Claude models in `config/models.yaml`. OpenAI, Gemini and other OpenAI-compatible endpoints work the same way once you add an entry for the model you want; see [Models and providers](models.md#adding-a-model-from-another-provider).

> **A `.env` file is not loaded automatically.** The kit reads real environment variables. To keep keys in a `.env` file, either export them in your shell or run commands through uv: `uv run --env-file .env agentkit run ...`. `.env` is git-ignored.

## 7. See what happened

Every run ends with a summary line and a run ID:

```
[completed] 2 steps | 1 tool calls | 1337 in / 43 out tokens | $0.0000 | 7.3s | trace: 8d042825...
```

Print the timeline of that run with the start of its ID:

```bash
uv run agentkit trace 8d042825 --project-dir my-agent
```

```
    0.00s  run        my_agent                        7251.2ms  2 steps, 1 tool calls, stop: completed
    0.00s    model_call local/default                   5241.2ms  643 in / 30 out, $0.0000, stop: tool_use
    5.24s    tool_call  km_to_miles                        2.5ms  {"km": 10} -> ok
    5.25s    model_call local/default                   2002.2ms  694 in / 13 out, $0.0000, stop: end
```

The run, the two model calls and the tool call each have a line, nested by what called what. Problems are also logged to stderr as JSON lines carrying the same run ID. See [Observability](observability.md).

## 8. Evaluate the real model

Delete `fake_model` from a case and it runs against the configured model. A real model can fail a case the script passes, for example by doing the conversion in its head instead of calling the tool. Finding that out is what the eval is for.

To evaluate a real model without paying for it every time, record its answers once and replay them:

```bash
uv run agentkit eval run my-agent/evals/units.yaml --project-dir my-agent --record
uv run agentkit eval run my-agent/evals/units.yaml --project-dir my-agent --replay
```

After changing a prompt, a tool or the model, run the suite again and compare it with the earlier run:

```bash
uv run agentkit eval compare <earlier run id> latest --project-dir my-agent
```

`compare` lists the cases that got better or worse, and exits with an error if the overall pass rate dropped by more than two points.

## 9. Have a conversation

`run` answers one request. `chat` keeps the conversation, so each message sees the earlier ones:

```bash
uv run agentkit chat my_agent --project-dir my-agent
```

```
Session 3f9a1c0b7d2e. Type /reset to forget the conversation, /exit to leave.
you> What is 17 times 23?
  [calculator {"expression": "17 * 23"}]
17 times 23 is 391.
you> Now add 9 to that.
  [calculator {"expression": "391 + 9"}]
Adding 9 to 391 results in 400.
```

Whether the model calls the calculator or answers on its own varies from model to model and run to run; the tool lines appear only when it does.

The history is stored in `.agentkit/memory.db`, so `--session <id>` continues a conversation in another invocation. See [Sessions and chat](sessions.md).

## Where to go next

- **Understand the design:** [Concepts](concepts.md).
- **More than one agent:** [Patterns](patterns.md), then the projects in [`examples/`](../examples).
- **Tools that change things:** [Writing tools](writing-tools.md) explains approvals, and [Guardrails](guardrails.md) explains limits.
- **Your own application:** [Using agentkit as a library](library.md).
- **Something not working:** [Troubleshooting](troubleshooting.md).
