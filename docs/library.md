# Using agentkit as a library

The command line is a thin layer over a Python API. This page shows how to use that API directly, to run agents inside your own application, stream their progress, ask a person for approval from a web front end, and keep conversations.

All of the examples assume a project directory `my-agent/` created with `agentkit new`. Everything is `async`; the CLI wraps it in `asyncio.run`, and so can you.

## Run an agent

```python
import asyncio
from pathlib import Path

from agentkit.config import load_config
from agentkit.patterns import run_named
from agentkit.runtime.adapters import get_adapter
from agentkit.runtime.context import RunContext
from agentkit.runtime.project import load_project

ROOT = Path("my-agent")


async def main() -> None:
    cfg = load_config(ROOT)                                    # layered configuration
    project = load_project(ROOT, cfg)                          # agents, tools and schemas
    adapter = get_adapter(cfg, project.registry, project.root, agents=project.agents)
    ctx = RunContext(cfg)                                      # one per run

    result = await run_named(project, adapter, "my_agent", "What is 17 times 23?", ctx)

    print(result.output)             # the answer
    print(result.stop_reason)        # completed, budget_exceeded, stalled, error, awaiting_approval
    print(result.usage.cost_usd, result.steps, result.tool_calls)
    print(result.trace_path)         # .agentkit/traces/<run_id>.jsonl


asyncio.run(main())
```

What each step does:

- **`load_config(project_dir)`** merges the five configuration sources and validates them. It raises `ConfigError` for anything invalid. Pass `env="prod"` to choose an environment, or `overrides={"limits": {"max_steps": 5}}` to override keys in code.
- **`load_project(root, cfg)`** imports the `tools/` and `agents/` folders of the project and returns a `Project`: `project.agents` (name to `AgentSpec`), `project.patterns` (workflows, routers and so on), `project.registry` (every enabled and project tool) and `project.schemas` (Pydantic models by name).
- **`get_adapter(...)`** returns the runtime named by `runtime.adapter`. Pass `agents=project.agents` so hand-offs can find their targets.
- **`RunContext(cfg)`** is one run's state: its ID, budget, approval channel, checks, tracer and logger. Create a new one for every run. Do not reuse one for a second run: it holds that run's budget.
- **`run_named`** runs whatever the project defines under that name, a single agent or a pattern.

`RunResult` fields:

| Field | Meaning |
|---|---|
| `output` | The answer. A string, or a Pydantic model if the agent has an `output_schema`. |
| `stop_reason` | `completed`, `budget_exceeded`, `stalled`, `awaiting_approval` or `error`. |
| `error` | `None`, or an `ErrorInfo` with a `kind` and a `message`. |
| `usage` | Totals: `input_tokens`, `output_tokens`, `cached_input_tokens`, `cost_usd`. |
| `steps`, `tool_calls`, `duration_seconds` | Counters for the run. |
| `messages` | The full transcript, including the system prompt. |
| `run_id`, `trace_path` | Identify the run and its trace file. |
| `pending` | Set when `stop_reason` is `awaiting_approval`; see below. |

A result that is not `completed` is not an exception. Check `stop_reason`. Exceptions (`AgentKitError` and its subclasses) are for set-up mistakes: an unknown agent name, a missing prompt file, a model that cannot do what the agent needs.

> **One project at a time per process.** `load_project` imports the project's modules by the names `tools.x` and `agents.x`, and forgets the previous project's when you load another. Load one project, keep it, and use it for every run.

## Stream a run

`adapter.stream(spec, input, ctx)` yields events as they happen instead of waiting for the end:

```python
spec = project.agents["my_agent"]

async for event in adapter.stream(spec, "What is 17 times 23?", ctx):
    if event.type == "text":
        print("agent:", event.text)                       # text before a tool call
    elif event.type == "tool_call":
        print("calling", event.tool_call.name, event.tool_call.arguments)
    elif event.type == "tool_result":
        print("result:", event.tool_result.content[:80])
    elif event.type == "done":
        print(event.result.output)                        # the final RunResult
```

The events are `text`, `tool_call`, `tool_result` and a final `done` that carries the `RunResult`. Streaming works at the level of steps: you see each tool call and result as it happens, and the final answer arrives with `done`.

## Approvals

By default a `RunContext` denies every tool call that needs approval, so nothing risky runs unattended. To let a person decide, give the run a channel.

### Decide immediately

A callback channel calls a function you provide for each request:

```python
from agentkit.guardrails.approvals import ApprovalDecision, ApprovalRequest, CallbackApproval


async def decide(request: ApprovalRequest) -> ApprovalDecision:
    # request.tool_name, request.arguments, request.side_effects, request.reason
    if request.tool_name == "write_file" and request.arguments["path"].startswith("reports/"):
        return ApprovalDecision(outcome="approve")
    return ApprovalDecision(outcome="deny", note="Only reports/ may be written.")


ctx = RunContext(cfg, approvals=CallbackApproval(decide))
```

The outcome is `approve`, `deny`, or `edit` with `edited_arguments` to run with different arguments instead. The note of a denial is shown to the agent. `request.reason` is the sentence the agent wrote just before the call.

To let an agent ask the operator a question through the `ask_human` tool, pass a second function: `CallbackApproval(decide, ask)`, where `ask` takes the question and returns the answer.

### Decide later

A web application cannot hold a request open while a person thinks. Return `None` from the callback and the run stops with `awaiting_approval`, handing you everything needed to continue:

```python
async def decide_later(request: ApprovalRequest) -> None:
    return None

ctx = RunContext(cfg, approvals=CallbackApproval(decide_later))
result = await adapter.run(spec, "Save the report", ctx)

if result.stop_reason == "awaiting_approval":
    request = result.pending.request                  # what to show the approver
    stored = result.pending.model_dump_json()         # plain data: keep it in your database
```

Later, when the person has answered:

```python
from agentkit.runtime.spec import PendingApproval

pending = PendingApproval.model_validate_json(stored)
ctx = RunContext(cfg, run_id=result.run_id)           # same run, so the trace continues
result = await adapter.resume(spec, pending, ApprovalDecision(outcome="approve"), ctx)
```

The run continues from the exact tool call, with its step and tool-call counts carried over. This works on the native runtime only.

The built-in channels are `CliApproval()`, `AutoDeny()` and `AutoApprove(env)`; `make_channel(cfg)` builds the one `guardrails.approval_channel` names.

## Sessions

To keep a conversation, use a session store and run through `run_in_session`:

```python
from agentkit.memory import make_stores
from agentkit.runtime.session import run_in_session

sessions, state = make_stores(cfg)                    # SQLite or in-memory, per memory.store
spec = project.agents["my_agent"]

for text in ["What is 17 times 23?", "Now add 9 to that."]:
    ctx = RunContext(cfg, session_id="demo", state=state)
    result = await run_in_session(adapter, spec, text, ctx, sessions)
    print(result.output)
```

Each call is its own run, so it needs its own `RunContext`, with the same `session_id`. `stream_in_session` is the streaming form. See [Sessions and chat](sessions.md) for what is stored and how long histories are trimmed. Pass `state=state` so the `remember` and `recall` tools use the same store.

## Checks

Pass your own checks to run code on the input, tool output or final answer. `checks=` replaces the configured ones, so start from them to keep the redactor:

```python
from agentkit.guardrails.checks import Checks

checks = Checks.from_config(cfg)                      # the redactor, as configured
checks.final_answer.append(MyCheck())
ctx = RunContext(cfg, checks=checks)
```

See [Guardrails](guardrails.md#your-own-checks) for writing a check.

## Use a model directly

You do not need an agent to use the model layer:

```python
from agentkit.models import Message, ModelRequest, get_model

model = get_model("fast", cfg)                        # a role, from the configuration
response = await model.generate(
    ModelRequest(messages=[Message.from_text("user", "Reply with one word: ok")])
)
print(response.message.text, response.usage.cost_usd)
```

You get retry, timeout, fallback, capability checks and cost, but no budget: budgets belong to runs.

## Call a tool directly

`execute_tool` runs a tool the way an agent would, with validation, approval policy, timeout and truncation, and never raises for a tool failure:

```python
from agentkit.runtime.executor import execute_tool

tool = project.registry.get("calculator")
result = await execute_tool(tool, {"expression": "6 * 7"}, RunContext(cfg))
print(result.content)                                 # exactly what the model would see
print(result.error)                                   # None, or a ToolError with a kind and a message
```

## Build without a project folder

A project folder is a convenience. You can assemble the pieces in code:

```python
from agentkit.config import load_config
from agentkit.runtime.adapters.native import NativeAdapter
from agentkit.runtime.spec import AgentSpec
from agentkit.tools import ToolRegistry, tool


@tool(side_effects="none")
def shout(text: str) -> str:
    """Upper-case some text.

    Args:
        text: The text to upper-case.
    """
    return text.upper()


cfg = load_config(Path("."))
registry = ToolRegistry()
registry.register(shout)

spec = AgentSpec(name="loud", description="Shouts.", instructions="Use shout on what you are given.",
                 tools=["shout"])
adapter = NativeAdapter(registry)
result = await adapter.run(spec, "hello", RunContext(cfg))
```

`ToolRegistry.from_config(cfg)` gives you a registry that already holds the builtin tools enabled in the configuration, and `registry.load_mcp(name)` adds the tools of a configured MCP server.

## Run evals from code

```python
from agentkit.evals.dataset import load_suites
from agentkit.evals.report import render_report
from agentkit.evals.runner import run_suites

run = await run_suites(load_suites(ROOT / "evals"), project, repeat=3, tags=["smoke"])
print(render_report(run))
```

`run_suites` takes `repeat`, `tags`, `live` (use real tools) and `cassette` (`"record"` or `"replay"`). See [Writing evals](writing-evals.md).

## Logging and shutting down

```python
from agentkit.observability.logs import configure_logging

configure_logging(cfg)       # JSON lines on stderr, at observability.log_level
```

If your configuration starts MCP servers, their connections are closed when the process exits. To close them earlier, call `agentkit.tools.mcp.close_mcp_connections()`.

## Errors

| Exception | Raised when |
|---|---|
| `ConfigError` | Configuration cannot be read or is invalid. |
| `ModelError` | A model call failed. Its `retryable` attribute says whether trying again may help. |
| `UnsupportedCapability` | A request needs something the model does not support, for example tools. |
| `AgentKitError` | The base class of all of the above, and raised for other set-up mistakes. |

Every message says what to do next. Failures during a run, such as a tool error, a budget limit or a refused model answer, are reported in the result rather than raised.
