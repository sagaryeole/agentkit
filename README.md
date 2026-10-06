# agentkit

A reusable foundation for building AI agent projects. Tools, evals, model access, configuration, safety controls and tracing are written once here, so a new agent starts from a working, testable baseline instead of from an empty file.

- **Any model, by configuration.** Agent code asks for a model by role (`default`, `fast`, `reasoning`). Config maps the role to Anthropic, OpenAI, Gemini, an OpenAI-compatible endpoint, or a model on your own machine.
- **Safe by default.** A tool has side effects unless it says otherwise, side effects need approval, and every run has a budget.
- **Evals from the first run.** A new project ships with an eval suite that passes with no API key.
- **Everything is traced.** Each run writes a trace of its model and tool calls that you can read in the terminal.

The design is specified in [AGENTIC_FRAMEWORK_SPEC.md](AGENTIC_FRAMEWORK_SPEC.md).

## Install

You need [uv](https://docs.astral.sh/uv/). It installs Python 3.11 or newer for you.

```
git clone <this repository> agentkit
cd agentkit
uv sync
uv run agentkit doctor
```

`doctor` checks the configuration and tells you which credentials are missing. Nothing below needs any until the section on real models.

The commands in this guide are run from the checkout with `uv run agentkit ...`. To have `agentkit` on your PATH everywhere instead, install it as a tool: `uv tool install --editable '.[anthropic]'`.

## From nothing to a passing custom eval

### 1. Create a project

```
uv run agentkit new my-agent
```

This creates `my-agent/`:

```
my-agent/
├── agentkit.yaml        # this project's configuration
├── agents/main.py       # the agent: an AgentSpec
├── prompts/main.md      # its prompt
├── tools/text.py        # a project tool, word_count
├── evals/smoke.yaml     # three starter eval cases
└── tests/
```

### 2. Run its evals

```
uv run agentkit eval run --project-dir my-agent
```

```
Suite my_agent_smoke (agent: my_agent)
  pass rate: 100.0% (3/3 attempts, 3 cases)
```

The starter cases script the model's replies, so they pass with no network and no key. They check that the prompt loads, the tools are wired and the limits hold.

### 3. Add a tool

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

Give it to the agent by adding its name to `tools` in `my-agent/agents/main.py`:

```python
    tools=["calculator", "word_count", "km_to_miles"],
```

Check what the model will be told about it, and call it once:

```
uv run agentkit tools describe km_to_miles --project-dir my-agent
uv run agentkit tools call km_to_miles --args '{"km": 10}' --project-dir my-agent
```

### 4. Write an eval for it

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

### 5. Run it

```
uv run agentkit eval run my-agent/evals/units.yaml --project-dir my-agent
```

```
Suite units (agent: my_agent)
  pass rate: 100.0% (1/1 attempts, 1 cases)
```

That is a passing custom eval. Because the model's replies are scripted, it proves the tool is registered, validated and reachable by the agent. The next section runs the same agent on a real model.

## Use a real model

### A model on your machine

With LM Studio or Ollama serving a model that supports tool calling, no cloud account is needed:

```
uv run agentkit models list --local          # IDs the server offers
export MODEL_PROVIDER=local LOCAL_MODEL_ID=<one of those IDs>
uv run agentkit models test default
uv run agentkit run my_agent "How many miles is 10 km?" --project-dir my-agent
```

LM Studio is the default server. For Ollama, set `models.local.server: ollama` in `agentkit.yaml`, or `AGENTKIT_MODELS__LOCAL__SERVER=ollama`.

### Anthropic

```
uv sync --extra anthropic
export ANTHROPIC_API_KEY=...
uv run agentkit run my_agent "How many miles is 10 km?" --project-dir my-agent
```

The roles map to Claude models in `config/models.yaml`. OpenAI, Gemini and other OpenAI-compatible endpoints are supported too; add an entry for the model you want as that file describes.

### See what happened

Every run ends with a summary line and a run ID:

```
[completed] 2 steps | 1 tool calls | 937 in / 58 out tokens | $0.0000 | 6.3s | trace: 2b7fc739...
```

```
uv run agentkit trace 2b7fc739 --project-dir my-agent
```

```
    0.00s  run        my_agent                        6343.8ms  2 steps, 1 tool calls, stop: completed
    0.00s    model_call local/default                   4842.7ms  439 in / 35 out, $0.0000, stop: tool_use
    4.84s    tool_call  km_to_miles                        1.9ms  {"km": 10} -> ok
    4.85s    model_call local/default                   1495.3ms  498 in / 23 out, $0.0000, stop: end
```

### Evaluate the real model

Delete `fake_model` from a case and it runs on the configured model. A real model can fail a case the script passes, for instance by doing the conversion in its head instead of calling the tool; finding that out is what the eval is for. Or keep the script for fast checks and record the real model's answers once, then replay them for free:

```
uv run agentkit eval run my-agent/evals/units.yaml --project-dir my-agent --record
uv run agentkit eval run my-agent/evals/units.yaml --project-dir my-agent --replay
```

After changing a prompt, a tool or the model, run the suite again and compare:

```
uv run agentkit eval compare <earlier run id> latest --project-dir my-agent
```

## How a project is put together

**Agents** are described, not programmed:

```python
from agentkit.runtime.spec import AgentSpec

agent = AgentSpec(
    name="my_agent",
    description="Answers questions, using tools for calculations and text statistics.",
    instructions="prompts/main.md",
    tools=["calculator", "word_count"],
)
```

**Configuration** is layered, later sources overriding earlier ones: the kit's defaults, the project's `agentkit.yaml`, `agentkit.<env>.yaml`, `AGENTKIT_*` environment variables, then command-line options. An unknown key is an error. `uv run agentkit config show --project-dir my-agent` prints the merged result.

**Limits** apply to every run: steps, tool calls, tokens, cost and wall time, under `limits` in the config. A run that reaches one stops and says which.

**Approvals.** A tool that changes something waits for the operator at the terminal. See [docs/patterns.md](docs/patterns.md#human-in-the-loop).

**Builtin tools** are enabled by name under `tools.enabled`: `calculator`, `current_time`, `read_file`, `list_files`, `search_files`, `write_file`, `http_fetch`, `web_search`, `sql_query`, `ask_human`, `remember`, `recall`, `finish`. `uv run agentkit tools list` shows the enabled ones.

## Commands

| Command | Does |
|---|---|
| `agentkit new <name> [--pattern single\|router\|workflow]` | Create a project. |
| `agentkit run <agent> "<input>" [--model <role>]` | Run an agent or workflow once. |
| `agentkit trace <run id>` | Show the timeline of a run. |
| `agentkit eval run [path] [--tags ...] [--repeat N] [--live] [--record\|--replay]` | Run eval suites. |
| `agentkit eval report [run]`, `agentkit eval compare <baseline> <candidate>` | Read and compare results. |
| `agentkit tools list`, `describe <tool>`, `call <tool> --args '{...}'` | Inspect and try tools. |
| `agentkit models list [--local]`, `agentkit models test <role>` | See and check models. |
| `agentkit config show`, `agentkit config validate` | Inspect configuration. |
| `agentkit doctor` | Check configuration, credentials and the local model server. |

Every command takes `--project-dir <dir>` and `--env <name>`.

## Going further

- [docs/patterns.md](docs/patterns.md): which agent shape to use, and when not to add more agents.
- [docs/writing-tools.md](docs/writing-tools.md): the tool contract, failures, and why tool output is untrusted.
- [docs/writing-evals.md](docs/writing-evals.md): the dataset format, graders, fixtures, record and replay.
- [SECURITY.md](SECURITY.md): the threat model.
- `examples/`: complete projects with eval suites: `single_agent`, `builtin_tools`, `router`, `workflow`, `human_in_loop`.

To run an agent on Google ADK instead of the kit's own loop, install the `adk` extra (`uv sync --extra adk`) and set `runtime.adapter: adk`.

## Limitations

- The Anthropic, OpenAI and Gemini providers are covered by unit tests but have not been exercised against the live APIs. The local-model path has.
- There is no conversation memory across runs yet: no session history, no trimming of long histories, and no `agentkit chat`.
- Not built: MCP tool loading, a tool that executes code, an OTLP trace exporter, and the planner/executor, parallel and evaluator/reviser patterns.

## Development

```
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

`CLAUDE.md` describes the architecture for contributors.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
