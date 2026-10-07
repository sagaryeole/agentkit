# Sessions and chat

`agentkit run` answers one request. A **session** is a conversation made of several requests, where each one can see the earlier ones. This page covers how history is kept, how `agentkit chat` uses it, and what the kit does when a conversation grows too long for the model.

> Sessions, context trimming and `agentkit chat` work on the native runtime. The [ADK adapter](adk.md) takes a single string and keeps no history.

## How a conversation works

A conversation is a series of ordinary runs. Each message you send is one run, with its own run ID, its own budget and its own trace. Before the run, the kit loads the session's stored history and puts the new message after it. After the run, it stores the turn: your message, the agent's tool calls and results, and its answer.

This has useful consequences:

- **Limits apply per message**, not to the whole conversation. A long chat does not use up one budget.
- **Every message is traceable** on its own with `agentkit trace`.
- **A turn that does not finish is stored as a short note.** If a message ends in an error or hits a budget, the history gets your message and a line such as `[This request was not completed: budget_exceeded.]`, so a half-finished tool exchange never enters the history.
- **System prompts are not stored.** Each run adds the agent's own prompt, so changing a prompt takes effect in existing sessions.
- **Provider reasoning blocks are not stored.** Models that return their reasoning (Claude's thinking blocks, for example) have it sent back unchanged inside one run's tool loop, as the provider requires, but it is not kept across messages.

## `agentkit chat`

```bash
uv run agentkit chat my_agent --project-dir my-agent
```

```
Session 3f9a1c0b7d2e. Type /reset to forget the conversation, /exit to leave.
you> What is 17 times 23?
  [calculator {"expression": "17 * 23"}]
17 times 23 is 391.
[completed] 2 steps | 1 tool calls | 921 in / 44 out tokens | $0.0000 | 6.0s | trace: afdac400...
you> Now add 9 to that.
```

- Tool calls are shown as they happen, on stderr. The answer goes to stdout.
- `/reset` forgets the conversation. `/exit`, end-of-input or Ctrl-C at the prompt leaves.
- On leaving, it prints how to come back: `agentkit chat my_agent --session 3f9a1c0b7d2e`.
- `--session <id>` continues an existing session, and creates one with that ID if none exists. Use a name you will remember: `--session support-demo`.
- Tools that need approval ask you at the terminal, the same as in `run`.

## Where history is kept

`memory.store` chooses:

| Value | Behaviour |
|---|---|
| `sqlite` (default) | History and notes are kept in a SQLite file, `memory.sqlite_path` (`.agentkit/memory.db`). They survive between invocations. |
| `memory` | Kept in the process only. Gone when it exits. Useful in tests. |

The same store holds the notes written by the `remember` tool and read by `recall`, which are scoped to a session. In a one-off `run` with no session, they last for that run.

**The SQLite file stores conversations as they are, unredacted.** Redaction applies to traces and logs, not to stored history, so protect the file like the data it holds, and keep `.agentkit/` out of version control. The default `.gitignore` of a new project does.

## When the conversation gets too long

Models have a context window, and a long conversation with big tool results can fill it. Before every model call, the kit estimates the size of what it is about to send. While it is under `memory.context.trigger_ratio` (80%) of the model's declared `context_window`, it sends the full history. Past that, it sends a **trimmed copy**. The stored history and the run's transcript are never changed: only what is sent to the model.

`memory.context.strategy` chooses how to trim:

| Strategy | What it does | Cost |
|---|---|---|
| `drop_tool_results` (default) | Blanks the oldest tool results, one at a time, until the history fits. The tool calls stay and their results become `[An earlier tool result was removed to save space.]`, so the history stays well-formed. | None. |
| `summarize` | Replaces everything before a recent user message with a summary written by the `fast` role. The summary is cached and extended as the conversation grows. It cuts at the start of a turn, never between a tool call and its result. If no such cut point exists, it falls back to dropping tool results. | One extra model call when the summary needs updating. It counts toward the run's token and cost limits, but not as a step. |
| `off` | Sends everything. The model call may fail if it is too long. | None. |

In all strategies, the newest `memory.context.keep_recent_messages` (6) messages are never touched. Whenever history is trimmed, the provider's reasoning blocks are left out of the copy, because a provider only accepts them back in the exact history that produced them.

```yaml
memory:
  store: sqlite
  context:
    strategy: summarize
    trigger_ratio: 0.7
    keep_recent_messages: 8
```

A trimmed model call is marked `context_trimmed: true` in its trace span.

### Tuning

- **Declare the real context window.** The trigger is a share of the model's `context_window`. Local models default to 8,192 tokens, and a model loaded with more should say so under `capabilities.context_window`; see [Models and providers](models.md#declare-what-your-model-supports). With a wrong value the kit trims too early or too late.
- **The size is an estimate**, about four characters to a token, not a count from the provider. Leave headroom with a ratio below 1.
- **Prefer smaller tool results** where you can. Trimming blanks results; it is better if the tool returns less in the first place, for example with `read_file`'s `max_chars`.
- **Summaries are lossy.** They keep what the user asked for, decisions, names, numbers and open items, but not everything. For a conversation where exact old details matter, raise `keep_recent_messages` or write those details somewhere with the `remember` tool.

## Using sessions in code

[Using agentkit as a library](library.md#sessions) shows `run_in_session` and the session stores.
