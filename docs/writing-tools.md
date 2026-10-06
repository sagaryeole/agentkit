# Writing tools

A tool is a Python function the model can call. The kit turns the function into a schema the model sees, checks every call before it runs, and turns every failure into something the model can act on.

## A complete tool

```python
# tools/orders.py in your project
from agentkit.runtime.context import RunContext
from agentkit.tools import ToolFailure, tool


@tool(side_effects="none", idempotent=True, timeout_seconds=10, tags=["orders"])
def lookup_order(order_id: int, include_items: bool = False, *, ctx: RunContext) -> dict:
    """Look up one order by its number.

    Use this when the customer mentions an order number.
    Do not use it to search for orders by name or date.

    Args:
        order_id: The order number printed on the receipt.
        include_items: Also return the line items.
    """
    order = ORDERS.get(order_id)
    if order is None:
        raise ToolFailure("not_found", f"There is no order {order_id}. Ask the customer to check the number.")
    return order
```

Put the file in your project's `tools/` folder. Every `Tool` defined there is registered when the project loads. Then list the tool's name in the `tools` of each `AgentSpec` that should have it; an agent only gets the tools its spec names.

Check what the model will be told, and try a call, without running an agent:

```
agentkit tools describe lookup_order
agentkit tools call lookup_order --args '{"order_id": 1182}'
```

## What the decorator derives

| From | The kit takes |
|---|---|
| Function name | The tool name, unless you pass `name=`. It must be `snake_case` and unique. |
| Type hints | The input schema. Every argument needs a hint. Use `Annotated[int, Field(ge=1)]` for constraints. Arguments not in the signature are rejected. |
| Docstring, up to `Args:` | The description shown to the model. |
| Each `Args:` entry | That argument's description in the schema. |
| A parameter named `ctx` | Nothing shown to the model. It receives the `RunContext` at call time. |

A tool without a docstring is refused, because the docstring is the only thing that tells the model when to call it.

## Write the docstring for the model

The model chooses tools from their descriptions alone. Say four things:

1. What the tool does, in one sentence.
2. When to use it.
3. When not to use it, and what to use instead.
4. What each argument means, including its format.

## Declare side effects

`side_effects` decides whether a call needs approval. If you leave it out, the tool is treated as `write`.

| Value | Meaning | Default policy |
|---|---|---|
| `none` | Reads local state or computes. | Runs. |
| `read_external` | Reads from outside the machine. | Runs. |
| `write` | Changes something. | Needs approval. |
| `destructive` | Changes something that cannot be undone. | Needs approval. Can only be allowed by its own entry in `tools.approval.overrides`. |

The policy for a call is resolved most specific first: `tools.approval.overrides.<tool>`, then `tools.approval.by_side_effects.<class>`, then `tools.approval.default`. Each is `allow`, `require` or `deny`.

When the policy is `require`, the run's approval channel decides. A denial is not an exception: the model receives a `permission` error and carries on. In evals and other unattended runs the channel denies everything unless the eval case allows the tool.

The other decorator arguments:

- `idempotent=True` says that calling twice with the same arguments is harmless.
- `timeout_seconds` overrides `timeouts.tool_call_seconds` for this tool.
- `tags` let an agent select tools by group with `registry.select(tags=[...])`.
- `untrusted_output=True` is covered below.

## Failing

Raise `ToolFailure(kind, message)`. The message is read by the model, so say what it should do next.

| Kind | Use it when |
|---|---|
| `invalid_input` | The arguments are well-typed but wrong. |
| `not_found` | The thing asked for does not exist. |
| `permission` | The call is not allowed. |
| `timeout` | Something took too long. |
| `transient` | Trying again may work. Pass `retryable=True`. |
| `internal` | The tool is misconfigured or broken. |

You do not need to catch your own exceptions to protect the run. Anything that escapes a tool becomes an `internal` error and is logged; a tool never raises to the model. Wrong argument types never reach your function either: the kit validates first and returns `invalid_input`.

## Output

Return a string, a JSON-serialisable value, or a Pydantic model (or a list or dict of them). Output longer than `tools.max_output_chars` is cut with a marker that says how much was dropped. If your tool can return a lot, add arguments that let the model ask for a range, as `read_file` does with `offset` and `next_offset`.

## Tool output is untrusted

Whatever a tool returns goes into the model's context, and the model may follow instructions it finds there. A web page, a file, a database row or an email can contain text written by someone who wants the agent to do something else. This is prompt injection, and no check inside the model reliably prevents it.

So treat tool output as data, never as instructions, and design for the case where the model is fooled anyway:

- Set `untrusted_output=True` on any tool whose result contains content from outside the kit. The kit then hands the result to the model inside an `<untrusted_content>` block, followed by a line telling the model not to follow instructions inside it. That makes injection less likely to work. It does not make it impossible.
- Give each agent only the tools it needs. An agent that can read the web and also send email can be talked into sending what it read.
- Keep `write` and `destructive` tools behind approval. The approval prompt shows the arguments, so a person sees what an injected instruction is trying to do.
- Confine what a tool can reach. The builtin file tools refuse paths outside one root, `http_fetch` only fetches allowed domains and public addresses, and `sql_query` opens the database read-only. Do the same in your tools, in code, not in the docstring.
- Never put secrets in a tool's output, and never let a tool's arguments choose where secrets are sent.

## Settings

Tools read their configuration from `tools.settings.<name>` in the config, never from constants. Validate it with a small model so a typo is caught:

```python
class OrdersSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_base: str
    page_size: int = 50


settings = OrdersSettings.model_validate(ctx.config.tools.settings.get("orders", {}))
```

Secrets come from environment variables, not from YAML.

## Tools from MCP servers

Tools offered by a [Model Context Protocol](https://modelcontextprotocol.io) server can be used like your own. Install the extra (`uv sync --extra mcp`) and declare the server in `agentkit.yaml`:

```yaml
tools:
  mcp_servers:
    docs:
      command: ["python", "servers/docs.py"]     # a local server, over stdio
      pass_env: [DOCS_API_KEY]                   # environment variables it may see
      side_effects:
        search: read_external                    # by the server's own tool names
      timeout_seconds: 30                        # per call; default timeouts.tool_call_seconds
    tracker:
      url: https://mcp.example.com/mcp           # or a remote server, over HTTP
      headers_env: { Authorization: TRACKER_AUTH_HEADER }
```

Each tool is registered as `<server>_<tool>` in snake case, for example `docs_search`. Run `agentkit tools list` to see the names, then add the ones an agent needs to its `tools`.

What applies to them:

- **They are `write` unless you say otherwise.** A server's own hints about its tools are not trusted. Until a tool is listed under `side_effects`, every call needs approval.
- **Their output is always treated as untrusted** and delimited for the model.
- **Arguments are checked against the server's schema** before anything is sent, and approvals, timeouts, truncation, budgets and tracing work as for any tool.
- **Their descriptions come from the server** and go into the model's prompt. Only connect servers you trust; a malicious description is a prompt injection.
- **A local server sees a minimal environment** plus the variables named in `pass_env`.
- **The connection is kept open.** A server is connected the first time it is needed and stays connected until the process exits, so a local server starts once and can keep state between calls, including across the messages of `agentkit chat`. If a call fails because the server has gone away, that call returns a retryable error and the next call reconnects. Set `keep_alive: false` on a server to connect afresh for every call instead.
- **In evals** they follow the usual rule: they need a fixture, or `--live`.

## What `ctx` gives you

| Attribute | Use |
|---|---|
| `ctx.config` | The merged configuration. |
| `ctx.run_id`, `ctx.session_id` | Identifiers for logs and for keying state. |
| `ctx.state` | Key-value store scoped to run, session or user. |
| `ctx.budget` | What the run has used so far. |
| `ctx.approvals` | The channel to the operator; `await ctx.approvals.ask(question)` asks a question. |
| `ctx.logger` | A logger. |

## Sync or async

Either. A plain `def` runs in a thread so it does not block other work; use `async def` when the tool awaits I/O.

## Testing a tool

Call it the way an agent would, through the executor, so the test covers validation, policy, the timeout and error conversion:

```python
from agentkit.config import load_config
from agentkit.runtime.context import RunContext
from agentkit.runtime.executor import execute_tool


async def test_unknown_order_tells_the_model_what_to_do(tmp_path):
    ctx = RunContext(load_config(tmp_path, environ={}))
    result = await execute_tool(lookup_order, {"order_id": 404}, ctx)
    assert result.error.kind == "not_found"
    assert "check the number" in result.error.message
```

`result.content` is exactly the text the model receives. A `write` tool is denied in such a test unless the config allows it or you pass an approval channel to `RunContext`.

In evals, tools with side effects do not run for real: give them a fixture, as described in [writing-evals.md](writing-evals.md).

## Checklist

- The docstring says what the tool does, when to use it and when not to.
- `side_effects` is declared and honest.
- Every failure the model can cause raises `ToolFailure` with a message it can act on.
- External content is marked `untrusted_output=True`.
- What the tool can reach is limited in code.
- Large results can be fetched in pieces.
- There is a test through `execute_tool`, and an eval case that uses the tool.
