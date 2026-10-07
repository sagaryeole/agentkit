# CLI reference

```
agentkit [OPTIONS] COMMAND [ARGS]...
```

Run `agentkit --help`, or `agentkit <command> --help`, for the same information from the program itself.

## Conventions

- **Project directory.** Commands that read configuration take `--project-dir <dir>` (default: the current directory), the directory holding `agentkit.yaml`, `agents/`, `tools/` and `evals/`. Relative paths in configuration are relative to it.
- **Environment.** The same commands take `--env <name>`, which selects `agentkit.<env>.yaml` and overrides `AGENTKIT_ENV`.
- **Streams.** An agent's answer and other results go to stdout. Errors, progress, run summaries and log lines go to stderr, so `agentkit run ... > answer.txt` captures only the answer.
- **Exit codes.** `0` means success. `1` means the command failed: an invalid configuration, a failing check, a run that did not complete, an eval below its threshold. `2` is the argument error that the CLI framework reports for a missing or unknown option.
- **Logging.** Every command that loads configuration sets up logging at `observability.log_level`; see [Observability](../observability.md#logs).

`--env` and `--project-dir` are accepted by every command below except `new`, so they are not repeated in each table.

## `agentkit new`

Create a new agent project that runs and passes its starter evals straight away, with no API key.

```
agentkit new NAME [--pattern single|router|workflow] [--dir DIR]
```

| Argument or option | Meaning |
|---|---|
| `NAME` | Name of the project directory to create. Lower-case letters, digits, `-` and `_`, starting with a letter. The agent is named after it, with `-` turned into `_`. |
| `--pattern` | The agent shape to start from. `single` (default) is one agent with tools; `router` is a router and two specialists; `workflow` is two agents in a fixed order. |
| `--dir` | Parent directory to create the project in. Default: the current directory. |

Exits `1` if the name is not usable, the directory already exists, or the pattern is unknown.

## `agentkit run`

Run an agent or workflow once and print its answer.

```
agentkit run AGENT INPUT [--model ROLE]
```

| Argument or option | Meaning |
|---|---|
| `AGENT` | The name of an agent or pattern defined in the project's `agents/`. |
| `INPUT` | What to ask it. |
| `--model` | A model role or catalogue key to use instead of the agent's own. Applies to a single agent. |

Prints the answer to stdout, then any error and a one-line summary to stderr:

```
[completed] 2 steps | 1 tool calls | 937 in / 44 out tokens | $0.0000 | 6.3s | trace: 2b7fc739...
```

Tool calls that need approval ask on the terminal (`guardrails.approval_channel`). Exits `1` unless the run's stop reason is `completed`.

## `agentkit chat`

Talk to an agent. The conversation is kept, so it can be continued later. See [Sessions and chat](../sessions.md).

```
agentkit chat AGENT [--session ID]
```

| Argument or option | Meaning |
|---|---|
| `AGENT` | The name of an agent defined in `agents/`. |
| `--session` | The session to continue. By default, a new session with a generated ID is started. |

At the `you>` prompt: `/reset` forgets the conversation, `/exit` leaves. Each message is its own run; tool calls are shown as they happen, and each answer is followed by the run summary. On leaving it prints the command to continue the session.

## `agentkit trace`

Print the timeline of a run from its trace file. See [Observability](../observability.md#the-trace-viewer).

```
agentkit trace RUN_ID
```

`RUN_ID` is a run ID, or the start of one that identifies exactly one trace under `observability.jsonl_path`. Exits `1` if there is no match or more than one.

## `agentkit doctor`

Check configuration, credentials, and that each model in use can be reached.

```
agentkit doctor [--offline]
```

| Option | Meaning |
|---|---|
| `--offline` | Do not contact any model provider. Credentials and configuration are still checked. |

For every model reachable through the roles and fallbacks, it checks the credentials, then contacts the provider and reports whether it answers, accepts the key and knows the model ID. These checks do not generate text, so they cost nothing. Models with no credentials set are skipped with a warning. A local model server that is not running, or a model ID it does not have, is a failure.

```
[ ok ] config: valid (env: dev)
[warn] project: no agentkit.yaml in .; using shipped defaults
[ ok ] keys: credentials for 'anthropic/claude-sonnet-5-5' are set
[ ok ] anthropic/claude-sonnet-5-5: Anthropic API answers and accepts 'claude-sonnet-5-5'
```

Exits `1` if the configuration is invalid or any check fails. Missing credentials alone are warnings and exit `0`.

## `agentkit config`

### `agentkit config show`

Print the merged configuration as YAML. This is what the kit will actually use, after all five sources are merged and relative paths are made absolute.

### `agentkit config validate`

Check that the merged configuration is valid. Prints `Configuration is valid (env: dev).` and exits `0`, or prints each problem with its key path and exits `1`.

## `agentkit models`

### `agentkit models list`

```
agentkit models list [--local]
```

Show which model each role resolves to, with its provider and fallbacks.

| Option | Meaning |
|---|---|
| `--local` | Ask the local model server which model IDs it offers instead; use one for `LOCAL_MODEL_ID`. |

### `agentkit models test`

```
agentkit models test [ROLE]
```

Send one short request to a model and print the reply, tokens, cost and latency. `ROLE` is a role or a catalogue key and defaults to `default`. This makes a real call and costs a few tokens. Exits `1` if the call fails.

## `agentkit tools`

These commands load the project, so they list and call the project's own tools and the tools of configured MCP servers as well as the enabled builtin tools. Configured MCP servers are started to find their tools.

### `agentkit tools list`

List the tools with their side-effect class and the first line of their description.

### `agentkit tools describe`

```
agentkit tools describe NAME
```

Show what the model is told about a tool: its description and its input schema.

### `agentkit tools call`

```
agentkit tools call NAME [--args JSON]
```

Call a tool the way an agent would, with validation, approval policy, timeout and truncation, and print exactly what the model would receive.

| Option | Meaning |
|---|---|
| `--args` | The arguments, as a JSON object. Default `{}`. |

A tool that needs approval is denied here, since there is no run to ask. Exits `1` if the tool returns an error or the arguments are not a JSON object.

## `agentkit eval`

See [Writing evals](../writing-evals.md) for the dataset format and graders.

### `agentkit eval run`

Run eval suites, print a report and save the results as JSON.

```
agentkit eval run [PATH] [--tags TAGS] [--repeat N] [--live] [--record | --replay] [--fail-under PERCENT]
```

| Argument or option | Meaning |
|---|---|
| `PATH` | A suite file, or a folder of suites (`*.yaml`, `*.yml`, `*.jsonl`). Default: `evals.dataset_dir`. |
| `--tags` | Run only cases with at least one of these comma-separated tags. |
| `--repeat` | Run each case this many times and report the pass rate per case. Default `1`. |
| `--live` | Use the real tools instead of fixtures and fakes. Side-effecting tools still need a case to allow them. |
| `--record` | Call the configured model and record its responses to a cassette file, replacing the suite's earlier cassette. |
| `--replay` | Answer from the recorded cassette. No network and no key. |
| `--fail-under` | Exit `1` when the pass rate, in percent, is below this. Default `evals.fail_under` (100). |

`--record` and `--replay` cannot be combined. Results are saved under `evals.results_dir` and the path is printed. Exits `1` when the pass rate is below the threshold, when no cases matched, or when a suite cannot be loaded.

### `agentkit eval report`

```
agentkit eval report [RUN]
```

Print the report of a saved run. `RUN` is a run ID, the start of one, a file path, or `latest` (the default).

### `agentkit eval compare`

```
agentkit eval compare BASELINE CANDIDATE [--max-drop POINTS]
```

Show which cases regressed, improved, appeared or disappeared between two saved runs. Both arguments accept what `eval report` accepts.

| Option | Meaning |
|---|---|
| `--max-drop` | Exit `1` if the pass rate fell by more than this many points. Default `evals.max_pass_rate_drop` (2). |

## Environment variables

The CLI reads the variables listed in [Configuration](../configuration.md#environment-variables), notably `AGENTKIT_ENV` and any `AGENTKIT_*` override, the provider keys, and `MODEL_PROVIDER` with `LOCAL_MODEL_ID` for local models.
