# Observability

Every run has an ID. Every run, model call and tool call is recorded as a span with its inputs, outputs, duration, tokens and cost, and logs carry the same run ID so a log line can be matched to its trace. Nothing needs a service to run: by default, traces are files in your project.

## What you see after a run

`agentkit run` and `agentkit chat` end each run with a summary line on stderr:

```
[completed] 2 steps | 1 tool calls | 937 in / 44 out tokens | $0.0000 | 6.3s | trace: 2b7fc739303a4452b8df6b03613d61f6
```

The fields are the stop reason, model turns, tool calls, input and output tokens, cost (`unpriced` for models with no price), duration and the run ID.

## The trace viewer

```bash
uv run agentkit trace 2b7fc739 --project-dir my-agent
```

You can give the start of a run ID, as long as it identifies one trace. The output is a timeline:

```
    0.00s  run        my_agent                        7251.2ms  2 steps, 1 tool calls, stop: completed
    0.00s    model_call local/default                   5241.2ms  643 in / 30 out, $0.0000, stop: tool_use
    5.24s    tool_call  km_to_miles                        2.5ms  {"km": 10} -> ok
    5.25s    model_call local/default                   2002.2ms  694 in / 13 out, $0.0000, stop: end
```

Reading across: when the span started relative to the run, an `!` if it failed, then the span's kind and name indented by nesting, how long it took, and a summary. For a model call the summary is the tokens, cost and the reason it stopped (`tool_use` means it asked for a tool). For a tool call it is the arguments and the outcome: `ok`, or the error kind such as `not_found` or `permission`, plus the approval decision if there was one (`approval: denied`).

In a pattern, the agents appear as nested `run` spans:

```
    0.00s  run        support                        13650.6ms  3 steps, 2 tool calls, stop: completed
    0.00s    model_call local/default                   6192.3ms  481 in / 65 out, $0.0000, stop: tool_use
    6.19s    tool_call  handoff                            1.4ms  {"agent": "billing", ...} -> ok
    6.20s    run        billing                         7454.7ms  3 steps, 2 tool calls, stop: completed
    6.20s      model_call local/default                   3145.7ms  474 in / 33 out, $0.0000, stop: tool_use
    9.34s      tool_call  calculator                         0.6ms  {"expression": "19.99 * 2"} -> ok
```

## Trace files

By default each run writes one file, `.agentkit/traces/<run_id>.jsonl`, with one JSON object per line. A span is written when it ends, so a parent comes after its children; the `seq` field records the order spans started, and the viewer sorts by it.

```json
{"span_id": "5d1f...", "seq": 3, "parent_id": "9a07...", "kind": "tool_call", "name": "km_to_miles",
 "run_id": "8d04...", "session_id": null, "agent_name": "my_agent", "env": "dev",
 "start": "2026-10-06T19:12:04.512+00:00", "duration_ms": 2.5, "status": "ok",
 "attributes": {"side_effects": "none", "truncated": false, "approval": null},
 "content": {"arguments": {"km": 10}, "result": "6.21"}}
```

| Field | Meaning |
|---|---|
| `span_id`, `parent_id` | The span, and the span that contains it (`null` for the top). |
| `kind` | `run`, `model_call` or `tool_call`. |
| `name` | The agent, model key or tool name. |
| `run_id`, `session_id`, `agent_name`, `env` | On every span, so spans can be joined across files and systems. |
| `start`, `duration_ms` | When it began and how long it took. |
| `status` | `ok` or `error`. An error span has an `error` attribute. |
| `attributes` | Metadata, listed below. |
| `content` | Inputs and outputs. Absent when `capture_content` is `false`. |

Attributes by kind:

| Kind | Attributes |
|---|---|
| `run` | `model`, `stop_reason`, `steps`, `tool_calls`, `input_tokens`, `output_tokens`, `cost_usd`; `pattern` for a pattern's run and `adapter` on ADK. |
| `model_call` | `model_id`, `stop_reason`, `input_tokens`, `output_tokens`, `cached_input_tokens`, `cost_usd`, `context_trimmed`; `purpose` when it is not an agent step, for example `summarize_context`. |
| `tool_call` | `side_effects`, `truncated`, `approval` (`approved`, `edited`, `denied`, `denied_by_policy` or `pending`), `error_kind`. |

Content by kind: a run records its `input` and `output`; a model call records the `messages` sent and the `response`; a tool call records its `arguments` and the `result` or `error`, plus `edited_arguments` if an approver changed them.

Because the files are plain JSON lines, ordinary tools work on them:

```bash
# total cost of every traced run (sum the model calls; a nested run reports a running total)
cat .agentkit/traces/*.jsonl | jq -s '[.[] | select(.kind=="model_call") | .attributes.cost_usd // 0] | add'

# every failed tool call, with its error
cat .agentkit/traces/*.jsonl | jq -c 'select(.kind=="tool_call" and .attributes.error_kind) | {run_id, name, error: .content.error}'
```

## Exporters

`observability.exporter` chooses where spans go:

| Value | Behaviour |
|---|---|
| `jsonl` | One file per run under `observability.jsonl_path`. The default. |
| `console` | Each span as a JSON line on stderr. Useful in a container where files are awkward. |
| `none` | The kit writes nothing itself. |

An `otlp` exporter is named in the schema but is not shipped yet; selecting it fails at start-up with a message saying so.

### OpenTelemetry

Whatever the exporter, every span is also created through the OpenTelemetry API, as `agentkit.<attribute>` attributes on a span named `<kind> <name>` (for example `tool_call km_to_miles`), plus `agentkit.run_id`, `agentkit.agent_name`, `agentkit.session_id` and `agentkit.env`. The API is a no-op unless your application installs an OpenTelemetry SDK and an exporter, in which case the spans reach any OTLP-compatible backend with no further setup. The kit's dependency is only on `opentelemetry-api`.

## Privacy: redaction and `capture_content`

Two controls decide what a trace may contain:

- **Redaction runs before anything is written.** The values of secret-looking environment variables and anything matching `guardrails.redact_patterns` are replaced with `[redacted:...]` in every span and log line. A test plants a secret in a prompt, a tool result and an environment variable and checks that it is in no trace.
- **`observability.capture_content: false`** goes further and records metadata only: durations, token counts, stop reasons and names, but none of the prompts, arguments or results. Use it for sensitive deployments.

Traces and logs can still contain business data that no pattern recognises. Treat the trace directory like the data your agent handles.

## Logs

Problems are logged to stderr as JSON lines, each carrying the `run_id` of the run that caused it:

```json
{"time": "2026-10-06T19:12:04.512+00:00", "level": "WARNING", "logger": "agentkit.run", "run_id": "2b7fc739...", "message": "tool read_file failed (not_found): 'todo.txt' is not a file. Use list_files to see what exists."}
```

| Level | What is logged |
|---|---|
| `DEBUG` | The start and end of every run. |
| `WARNING` | A failed tool call, and a run that stopped without completing (`budget_exceeded`, `stalled`, `awaiting_approval`). |
| `ERROR` | A run that ended in `error`, and an unexpected exception inside a tool, with its traceback. |

A run that completes logs nothing at the default level, so output stays quiet. Set `observability.log_level` to change that. Log lines are redacted the same way as traces. To follow a log line to the full story, take its `run_id` to `agentkit trace`.

The CLI configures logging when it loads the configuration. In your own program, call it once:

```python
from agentkit.observability.logs import configure_logging

configure_logging(cfg)                     # to stderr, at observability.log_level
```

Until you call it, nothing is configured, and Python's default handler prints only warnings and errors, as plain text, to stderr.

The kit's logger is named `agentkit`, and once configured it does not pass records on to the root logger. If your application has its own logging setup, skip `configure_logging` and attach your handlers to the `agentkit` logger instead. Code that you write inside a tool should log through `ctx.logger`, which stamps the run ID on each record.

## Reading a trace to debug a run

When a run does something unexpected, work from the outside in:

1. **Check the stop reason** in the `run` span. `budget_exceeded`, `stalled` and `error` each have an error attribute saying why.
2. **Walk the timeline.** Which tool calls happened, in what order, and did any fail? A `permission` error means an approval was denied; `not_found` and `invalid_input` mean the model gave a tool bad arguments, which usually points at a tool description that is not clear enough.
3. **Open a model call's `content`** to see exactly what the model was sent and what it said. If `context_trimmed` is true, the history was trimmed for that call.
4. **Compare token counts** between model calls. A large jump shows a big tool result; consider asking the tool for less.
