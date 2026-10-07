# Guardrails

Guardrails are what make an agent safe to leave running. This page covers the budget that bounds every run, the detection of loops and stalls, the approval gate for risky tool calls, and the checks that clean what goes in and out. For the threat model behind them, read [SECURITY.md](../SECURITY.md).

## Budgets

Every run has a budget. It counts what the run has used and is checked **before every model call and every tool call**. When a limit is reached, the run stops with the stop reason `budget_exceeded`, the error names the limit that was hit, and whatever answer exists so far is returned.

```yaml
limits:
  max_steps: 25               # model turns
  max_tool_calls: 50
  max_input_tokens: 500000
  max_output_tokens: 50000
  max_cost_usd: 2.00
  max_wall_seconds: 300
```

```
budget_exceeded: The run reached limits.max_steps (3 of 3). Raise the limit in the config or the AgentSpec, or give the agent a smaller task.
```

Things worth knowing:

- **One budget per run, shared by everything in it.** In a pattern such as a router or a fan-out, every agent draws on the same budget, so the limits bound the whole pattern. A nested agent's step count is therefore the running total, not its own.
- **A single agent can have its own limits.** Set `limits=` on its `AgentSpec` (a `Limits` object) to replace the global limits for that agent. Nested agents inside a pattern use the pattern's budget.
- **Cost only counts priced models.** `limits.max_cost_usd` adds up the cost of calls to models that have a `pricing` entry. A model with no price contributes nothing, and a zero-cost run never trips a zero limit.
- **Limits are checked between calls, not during one.** A single long model call can exceed a token or time limit before the next check.
- **Summaries count.** When context trimming summarises old turns, that model call is counted against the run's token and cost limits but not as a step.

## Loop and stall detection

Two further limits catch an agent that is not getting anywhere:

| Key | Default | Detects |
|---|---|---|
| `guardrails.max_identical_tool_calls` | `3` | The same tool called with the same arguments more often than this. |
| `guardrails.max_empty_steps` | `2` | That many consecutive model turns with neither a tool call nor any text. |

Either ends the run with the stop reason `stalled`.

## Approvals

A tool call that could change something should not run unless a person agrees. Whether a call needs a decision is a matter of policy; who decides is the approval channel.

### Policy

Each tool declares what it can do, with `side_effects`:

| Class | Meaning | Default policy |
|---|---|---|
| `none` | Reads local state or computes. | `allow` |
| `read_external` | Reads from outside the machine. | `allow` |
| `write` | Changes something. The default for a tool that declares nothing. | `require` |
| `destructive` | Changes something that cannot be undone. | `require` |

The policy for a call is the first of these that applies:

1. `tools.approval.overrides` for the tool's name.
2. `tools.approval.by_side_effects` for its class.
3. `tools.approval.default`.

The values are `allow` (run it), `require` (ask the channel) and `deny` (refuse it). A `destructive` tool can only be allowed by an override naming that tool; allowing the class is ignored.

```yaml
tools:
  approval:
    default: require
    by_side_effects:
      none: allow
      read_external: allow
    overrides:
      write_file: allow          # I trust this one in this project
      issue_refund: deny
```

### Channels

When the policy is `require`, the run asks its approval channel. `guardrails.approval_channel` chooses one:

| Channel | Behaviour |
|---|---|
| `cli` | Asks at the terminal: approve, deny, or edit the arguments. The default for the command line. |
| `callback` | Calls a function you supply. For web, chat or other front ends. Needs code; see [Using agentkit as a library](library.md#approvals). |
| `auto_deny` | Denies everything. For unattended runs and evals. |
| `auto_approve` | Approves everything. For tests only, and refused when `env` is `prod`. |

When you create a run in code without choosing a channel, the default is `auto_deny`, so nothing with side effects runs unattended by accident.

The person approving sees the tool, its arguments, and the sentence the agent wrote just before the call. Tell your agents in the prompt to say what they are about to do before a side-effecting call, because that sentence is what the approver reads.

### What a denial does

A denial is not an error that stops the run. The model receives a `permission` error that says the operator did not approve, with their note if they gave one, and the run continues, so the agent can try something else or explain what it could not do. Every decision is recorded in the trace.

### Approvals that wait

A channel that cannot answer yet can return nothing. The run then stops with `awaiting_approval` and a `pending` value, plain data you can store. When the decision arrives, resume the run from exactly that tool call:

```python
result = await adapter.resume(spec, pending, ApprovalDecision(outcome="approve"), ctx)
```

This is how a web application can ask a person who is not in front of a terminal. See [Using agentkit as a library](library.md#approvals). Resuming works on the native runtime; the [ADK adapter](adk.md) cannot resume.

## Checks and redaction

A **check** is a small function that sees text at one of three points in a run and may rewrite or reject it:

| Point | Text |
|---|---|
| `user_input` | What the user sent. |
| `tool_output` | What a tool returned, before the model sees it. |
| `final_answer` | The agent's answer, before it is returned. |

A check returns the text to use, and optionally a rejection. A rejected input or answer ends the run with an `error` stop reason; a rejected tool output is withheld from the model and replaced by an error.

### The redactor

The kit ships a redactor. It removes the values of environment variables that look like secrets (names ending in `KEY`, `TOKEN`, `SECRET`, `PASSWORD` or `CREDENTIALS`) and anything that matches the patterns you name in `guardrails.redact_patterns`. Available patterns are `email` and `api_key` (the common key formats such as `sk-...`, `ghp_...`, `AKIA...` and bearer tokens). Matches become `[redacted:email]`, `[redacted:api_key]` or `[redacted:secret]`.

It is applied in two layers:

- **Traces and logs: always.** Nothing sensitive is written to disk or to the log.
- **What the model sees: where `guardrails.redact_at` says.** The default is `[tool_output, final_answer]`, so a secret that a tool returns never reaches the model and cannot be repeated to the user. The user's own input is left alone by default.

```yaml
guardrails:
  redact_patterns: [email, api_key]
  redact_at: [tool_output, final_answer]   # add user_input to redact what the user types
```

If a tool genuinely has to hand email addresses to the model, as a customer lookup does, remove `tool_output` from `redact_at`, or set it to `[]`.

Redaction is pattern based. It catches the shapes it knows and the values of secret-looking variables, and does not find every kind of personal data or credential. Treat it as one layer.

### Your own checks

A check is an async callable taking the text and the run context and returning a `CheckResult`:

```python
from agentkit.guardrails.checks import CheckResult, Checks
from agentkit.runtime.context import RunContext


class NoInternalCodes:
    async def __call__(self, text: str, ctx: RunContext) -> CheckResult:
        if "INTERNAL-" in text:
            return CheckResult(text=text, ok=False, reason="mentions an internal code")
        return CheckResult(text=text)


ctx = RunContext(cfg, checks=Checks(final_answer=[NoInternalCodes()]))
```

Passing `checks=` replaces the configured ones. To keep the redactor as well, add `Redactor(patterns, secrets)` to your lists, or build on `Checks.from_config(cfg)`. A `SchemaCheck(MyModel)` rejects text that is not JSON matching a Pydantic model; for the final answer you will usually set `output_schema` on the `AgentSpec` instead, which parses the answer into the model.

## Treat tool output as untrusted

Whatever a tool returns goes into the model's context, and the model may follow instructions it finds there. Tools that return outside content (web pages, files, database rows, MCP results) are marked `untrusted_output`, and the kit hands their results to the model in a delimited block with a note that the contents are data. That makes injected instructions less likely to work; it does not make it impossible. The real protections are the ones on this page: least privilege, approvals for side effects, confined tools and budgets. [Writing tools](writing-tools.md#tool-output-is-untrusted) explains how to design for it.

## Putting it together

A sensible starting point for a project that takes real actions:

```yaml
limits:
  max_steps: 15
  max_cost_usd: 0.50
  max_wall_seconds: 120

tools:
  approval:
    default: require              # anything new needs a decision
    overrides:
      read_file: allow
      lookup_order: allow

guardrails:
  approval_channel: cli           # or callback, in an application
  redact_at: [tool_output, final_answer]
```

and in the prompt: *before you call any tool that changes something, say in one sentence what you are about to do and why*.
