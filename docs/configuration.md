# Configuration

Everything you can tune in agentkit is a configuration key. This page explains how configuration is loaded and lists every key, its default and what it does, followed by every environment variable the kit reads.

## How configuration is loaded

Five sources are merged, later ones overriding earlier ones:

1. **The shipped defaults**: `config/default.yaml`, `config/models.yaml` (the `models` section) and `config/pricing.yaml` (the `pricing` section), which come with the kit.
2. **Your project's `agentkit.yaml`**, in the project directory.
3. **`agentkit.<env>.yaml`**, where `<env>` is the environment name from `AGENTKIT_ENV` or the `--env` option. Use it for settings that differ between, say, `dev` and `prod`.
4. **Environment variables** that start with `AGENTKIT_`.
5. **Arguments**: the `--env` option, or `overrides=` when you call `load_config` from code.

Mappings merge key by key, so your `agentkit.yaml` only needs the keys you change. Lists and single values replace the earlier value completely: setting `tools.enabled` replaces the whole list.

```yaml
# agentkit.yaml: only what differs from the defaults
limits:
  max_steps: 10
  max_cost_usd: 0.50

tools:
  enabled: [calculator, read_file, http_fetch]
  settings:
    http_fetch:
      allowed_domains: ["*.example.com"]
```

**Unknown keys are errors.** A misspelled key stops the program with a message naming it, instead of being silently ignored. The same applies to a stray `AGENTKIT_*` environment variable. Run `uv run agentkit config validate --project-dir <dir>` to check, and `uv run agentkit config show --project-dir <dir>` to print the merged result.

**Relative paths are relative to the project directory**, the one holding `agentkit.yaml`, not to where you ran the command. This applies to `observability.jsonl_path`, `evals.dataset_dir`, `evals.results_dir`, `evals.cassette_dir`, `memory.sqlite_path`, the `filesystem.root` setting and the `sql_query.database` setting. Absolute paths are left as they are.

### Overriding a key with an environment variable

Prefix the key with `AGENTKIT_`, upper-case it, and use a double underscore for each level of nesting. The value is read as YAML, so numbers, booleans and lists keep their type:

```bash
export AGENTKIT_LIMITS__MAX_STEPS=10
export AGENTKIT_OBSERVABILITY__CAPTURE_CONTENT=false
export AGENTKIT_TOOLS__ENABLED="[calculator, read_file]"
export AGENTKIT_MODELS__LOCAL__SERVER=ollama
```

### Environments

`env` names the environment, and `AGENTKIT_ENV` or `--env` selects `agentkit.<env>.yaml`. One rule depends on it: the `auto_approve` approval channel is refused when `env` is `prod`.

### Secrets

Secrets come from environment variables and never from YAML. The kit reads the variables listed under [Environment variables](#environment-variables). Before a trace or a log line is written, the values of environment variables that look like secrets (names ending in `KEY`, `TOKEN`, `SECRET`, `PASSWORD` or `CREDENTIALS`) are redacted, along with the patterns in `guardrails.redact_patterns`.

The kit does not read a `.env` file by itself. Export the variables in your shell, or run through uv: `uv run --env-file .env agentkit ...`.

## Reference

Defaults are the shipped values. Where a key takes a list or a mapping, the default is shown in YAML.

### `env`

| Key | Default | Meaning |
|---|---|---|
| `env` | `dev` | The environment name. Selects `agentkit.<env>.yaml`. `prod` forbids the `auto_approve` approval channel. |

### Models

See [Models and providers](models.md) for how these fit together.

| Key | Default | Meaning |
|---|---|---|
| `models.provider` | `null` | Set to `local` to move every hosted role to the local model. `MODEL_PROVIDER` overrides it. |
| `models.roles` | see below | Maps each role name to a key in `models.models`. |
| `models.fallbacks` | `{}` | For a role, an ordered list of catalogue keys to try when its model still fails after retries. |
| `models.models` | see below | The model catalogue, keyed by names you choose such as `anthropic/claude-sonnet-5-5`. |
| `models.local.server` | `lmstudio` | `lmstudio`, `ollama` or `custom`. Chooses the default base URL. |
| `models.local.api_base` | `null` | The server's base URL including port and `/v1`. Defaults from `server`. `LOCAL_API_BASE` overrides it. Required when `server` is `custom`. |
| `models.local.api_key` | `local` | A placeholder sent to the local server, which ignores it. `LOCAL_API_KEY` overrides it. |
| `models.local.request_timeout_seconds` | `300` | Timeout for a local model call, which is slower than a hosted one. |

The default roles:

```yaml
models:
  roles:
    default:   anthropic/claude-sonnet-5-5
    fast:      anthropic/claude-haiku-4-5
    reasoning: anthropic/claude-opus-5-5
    judge:     anthropic/claude-sonnet-5-5
```

A catalogue entry under `models.models` accepts:

| Field | Meaning |
|---|---|
| `provider` | `anthropic`, `openai`, `google`, `openai_compat`, `local` or `fake`. |
| `model_id` | The model's ID exactly as the provider names it. |
| `api_base` | For `openai`, `google` and `openai_compat`: the endpoint, including `/v1`. |
| `api_key_env` | For `openai`, `google` and `openai_compat`: the environment variable holding the key. |
| `defaults` | Request fields used when a request does not set them, such as `max_output_tokens`. |
| `capabilities` | What the model supports, overriding the provider's defaults: `tools`, `parallel_tool_calls`, `forced_tool_choice`, `structured_output`, `vision`, `streaming`, `prompt_caching`, `context_window`, `max_output_tokens`. |

### Pricing

`pricing` maps a model key, or `<provider>/*` for every model of a provider, to a price in US dollars per million tokens. Models with no entry get no cost figure. Add your own entries in `agentkit.yaml`:

```yaml
pricing:
  openai/my-model: { input: 1.00, output: 4.00, cached_input: 0.10 }
```

`cached_input` is optional and defaults to the `input` price. The shipped file prices the three Claude models and sets local and fake models to zero.

### Limits

Every run has a budget. A run that reaches any limit stops with `budget_exceeded` and names it. An `AgentSpec` can set its own limits. See [Guardrails](guardrails.md#budgets).

| Key | Default | Meaning |
|---|---|---|
| `limits.max_steps` | `25` | Model turns per run. |
| `limits.max_tool_calls` | `50` | Tool calls per run. |
| `limits.max_input_tokens` | `500000` | Input tokens summed over the run. |
| `limits.max_output_tokens` | `50000` | Output tokens summed over the run. |
| `limits.max_cost_usd` | `2.00` | Cost of the run. Only counts models that have a price. |
| `limits.max_wall_seconds` | `300` | Wall-clock time of the run, checked between calls. |

### Retry and timeouts

| Key | Default | Meaning |
|---|---|---|
| `retry.max_attempts` | `3` | Attempts per model, including the first. Only transient failures are retried. |
| `retry.base_delay_seconds` | `1.0` | First backoff delay. It doubles each attempt, with jitter. |
| `retry.max_delay_seconds` | `30.0` | Cap on the backoff delay. |
| `timeouts.model_call_seconds` | `120` | Timeout for one model call. |
| `timeouts.tool_call_seconds` | `60` | Timeout for one tool call, unless the tool sets its own. |

### Tools

See [Writing tools](writing-tools.md) and the [builtin tool reference](reference/builtin-tools.md).

| Key | Default | Meaning |
|---|---|---|
| `tools.enabled` | `[http_fetch, read_file, calculator]` | The builtin tools registered for the project. An agent still has to list a tool by name to use it. |
| `tools.max_tools_per_agent` | `20` | Selecting more tools than this for one agent logs a warning, because tool choice gets worse as the list grows. |
| `tools.max_output_chars` | `20000` | Tool output longer than this is cut, with a marker saying how much was dropped. |
| `tools.approval.default` | `require` | Policy for a tool whose class has no entry below: `allow`, `require` or `deny`. |
| `tools.approval.by_side_effects` | `none: allow`, `read_external: allow` | Policy by declared side-effect class: `none`, `read_external`, `write`, `destructive`. |
| `tools.approval.overrides` | `remember: allow` | Policy by tool name. The only way to allow a `destructive` tool. |
| `tools.settings` | see below | Settings of individual tools, keyed by tool name. |
| `tools.mcp_servers` | `{}` | MCP servers whose tools are registered. See below. |

Policy is resolved most specific first: `tools.approval.overrides`, then `tools.approval.by_side_effects`, then `tools.approval.default`.

Per-tool settings:

| Setting | Default | Used by |
|---|---|---|
| `tools.settings.http_fetch.allowed_domains` | `[]` | `http_fetch`. Nothing can be fetched until domains are listed. `*.example.com` also matches `example.com`; `*` allows every domain. |
| `tools.settings.filesystem.root` | `./workspace` | `read_file`, `list_files`, `search_files`, `write_file`. Every path is confined to this folder. |
| `tools.settings.web_search.backend` | `searxng` | `web_search`. Only `searxng` ships. |
| `tools.settings.web_search.base_url` | `null` | `web_search`. The address of your SearXNG instance. |
| `tools.settings.sql_query.database` | `null` | `sql_query`. Path of a SQLite file, opened read-only. |
| `tools.settings.sql_query.max_rows` | `1000` | `sql_query`. Upper limit on rows returned. |
| `tools.settings.sql_query.timeout_seconds` | `10` | `sql_query`. Upper limit on query time. |

Each entry under `tools.mcp_servers.<name>` takes exactly one of `command` or `url`, plus:

| Field | Meaning |
|---|---|
| `command` | A list: the program and arguments of a local server, spoken to over stdio. |
| `url` | The address of a remote server, spoken to over streamable HTTP. |
| `pass_env` | Environment variables a local server may see. It otherwise gets a minimal environment. |
| `headers_env` | For a remote server: HTTP header name mapped to the variable holding its value. |
| `side_effects` | Side-effect class per tool, by the server's own tool name. A tool not listed is treated as `write`. |
| `timeout_seconds` | Timeout per call. Defaults to `timeouts.tool_call_seconds`. |
| `keep_alive` | `true` (the default) keeps one connection open for the life of the process; `false` connects for every call. |

### Guardrails

See [Guardrails](guardrails.md).

| Key | Default | Meaning |
|---|---|---|
| `guardrails.approval_channel` | `cli` | Who decides a tool call that needs approval: `cli`, `callback`, `auto_deny` or `auto_approve`. `auto_approve` is for tests and is refused when `env` is `prod`. |
| `guardrails.redact_patterns` | `[email, api_key]` | What counts as sensitive. Available: `email`, `api_key`. Always applied to traces and logs. |
| `guardrails.redact_at` | `[tool_output, final_answer]` | Where the redactor also runs, so the model never sees or repeats what it removes. Any of `user_input`, `tool_output`, `final_answer`, or `[]`. |
| `guardrails.max_identical_tool_calls` | `3` | The same tool call with the same arguments more often than this stalls the run. |
| `guardrails.max_empty_steps` | `2` | Consecutive steps with neither a tool call nor an answer before the run stalls. |

### Observability

See [Observability](observability.md).

| Key | Default | Meaning |
|---|---|---|
| `observability.exporter` | `jsonl` | `jsonl` writes a trace file per run, `console` prints spans to stderr, `none` disables the kit's own exporter. `otlp` is accepted by the schema but rejected at start-up: it is not shipped yet. |
| `observability.jsonl_path` | `./.agentkit/traces` | Where trace files go. |
| `observability.log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. |
| `observability.capture_content` | `true` | `false` records metadata only: no prompts, tool arguments or outputs. |

### Evals

See [Writing evals](writing-evals.md).

| Key | Default | Meaning |
|---|---|---|
| `evals.dataset_dir` | `./evals` | Where `eval run` looks when given no path. |
| `evals.results_dir` | `./.agentkit/evals` | Where results are saved. |
| `evals.cassette_dir` | `./evals/cassettes` | Where recorded model responses are kept. |
| `evals.concurrency` | `4` | Cases run at the same time. |
| `evals.judge_role` | `judge` | The model role used by `llm_judge` graders. |
| `evals.judge_min_score` | `0.7` | An `llm_judge` score below this fails the case. |
| `evals.fail_under` | `100` | `eval run` exits with an error below this pass rate, in percent. |
| `evals.max_pass_rate_drop` | `2` | `eval compare` exits with an error when the pass rate falls by more than this many points. |

### Memory

See [Sessions and chat](sessions.md).

| Key | Default | Meaning |
|---|---|---|
| `memory.store` | `sqlite` | `sqlite` keeps sessions and notes in a file; `memory` keeps them for the life of the process. |
| `memory.sqlite_path` | `./.agentkit/memory.db` | The SQLite file. |
| `memory.context.strategy` | `drop_tool_results` | What to do when history nears the context window: `off`, `drop_tool_results` or `summarize`. |
| `memory.context.trigger_ratio` | `0.8` | Act when history reaches this share of the model's context window. |
| `memory.context.keep_recent_messages` | `6` | The newest messages are never trimmed. |

### Runtime

| Key | Default | Meaning |
|---|---|---|
| `runtime.adapter` | `native` | `native` or `adk`. `langgraph` and `claude_sdk` are accepted by the schema but are not shipped, and selecting them is an error. See [ADK adapter](adk.md). |

## Environment variables

| Variable | Meaning |
|---|---|
| `AGENTKIT_ENV` | The environment name. Selects `agentkit.<env>.yaml` and sets `env`. |
| `AGENTKIT_<KEY>` | Any configuration key, with `__` between levels, for example `AGENTKIT_LIMITS__MAX_STEPS=10`. |
| `ANTHROPIC_API_KEY` | Credentials for `anthropic` models. The Anthropic SDK also accepts `ANTHROPIC_AUTH_TOKEN` or a login profile created by its CLI. |
| `OPENAI_API_KEY` | Credentials for `openai` models. |
| `GOOGLE_API_KEY` | Credentials for `google` models. `GEMINI_API_KEY` is read too. |
| `MODEL_PROVIDER` | Set to `local` to move every hosted model role to the local model. |
| `<AGENT_NAME>_MODEL_PROVIDER` | The same switch for one agent, with the agent's name upper-cased and non-alphanumeric characters turned into underscores, for example `TRIAGE_MODEL_PROVIDER=local`. It overrides `MODEL_PROVIDER` in either direction. |
| `LOCAL_API_BASE` | Base URL of the local model server, including the port and `/v1`. |
| `LOCAL_MODEL_ID` | The model ID to use on the local server, exactly as `agentkit models list --local` shows it. |
| `LOCAL_API_KEY` | Optional key for the local server. A placeholder is sent when unset. |

Models in the catalogue can name their own key variable with `api_key_env`, and MCP servers read the variables named in `pass_env` and `headers_env`. Those are whatever you choose.
