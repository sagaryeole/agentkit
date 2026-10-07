# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`agentkit` is a reusable foundation for AI agent projects: a provider-independent model layer, tools, guardrails, a runtime with library adapters, observability, an eval harness, and a scaffold for new agents.

`AGENTIC_FRAMEWORK_SPEC.md` is the source of truth. Read the relevant section before implementing anything; it defines the interfaces, and section 17 defines the build order.

## Commands

```
uv sync                                   # install (Python 3.11+ is fetched by uv)
uv run pytest                             # all tests
uv run pytest tests/unit/test_config.py::test_unknown_key_fails   # one test
uv run ruff check . && uv run ruff format --check .
uv run mypy                               # --strict on src/agentkit

uv run agentkit config validate | show    # every command takes --env <name>, --project-dir <dir>
uv run agentkit doctor
uv run agentkit models list [--local] | test <role>
uv run agentkit tools list | describe <tool> | call <tool> --args '{"expression": "6*7"}'
uv run agentkit new <name> [--pattern single|router|workflow] [--dir <parent>]
uv run agentkit run <agent or workflow> "<input>" [--model <role>] --project-dir examples/single_agent
AGENTKIT_RUNTIME__ADAPTER=adk uv run agentkit run ...     # same agent on Google ADK
uv run agentkit chat <agent> [--session <id>]
uv run agentkit trace <run id>
uv run agentkit eval run [path] [--tags smoke] [--repeat N] [--live] [--record | --replay]
uv run agentkit eval report [run] | compare <baseline> <candidate>

# opt-in live tests (skipped when the server or key is absent)
MODEL_PROVIDER=local LOCAL_MODEL_ID=<id> uv run pytest tests/integration -m local_model
uv run pytest tests/integration -m integration        # needs ANTHROPIC_API_KEY
```

CI (`.github/workflows/ci.yml`) runs ruff, mypy, pytest, the smoke evals of all eight example projects (and of `single_agent` again on the ADK adapter), and a `pip-audit` scan of the locked dependencies.

## Build status

Milestones are built strictly in order; a milestone does not start until the previous one meets its acceptance criteria in spec section 17.

- **M1–M9: done**, with these gaps:
  - The Anthropic, OpenAI and Google providers have never been run against the real APIs (no credentials were available). Their request building and error mapping are unit-tested only. The Google provider goes through Gemini's OpenAI-compatible endpoint rather than its native API.
  - The ADK adapter has been run live only against a local model through LiteLLM. Its Gemini and Anthropic mappings are checked by a unit test, not by a call.
  - `web_search` (SearXNG) has only been tested against a mocked server.
  - `exporter: otlp` is rejected; only `jsonl`, `console` and `none` ship.
  - All eight patterns of spec section 15 exist. Planner/executor, fan-out and the revise loop have run live only on a local model.
  - MCP loading has been tested against a local stdio server; the HTTP transport has not been run.
  - `SECURITY.md` and `CODE_OF_CONDUCT.md` send reports through GitHub's private reporting; that has to be enabled on the repository once it is published.
  - Dropped or deferred by the owner: `run_python` (dropped for now), LangGraph and Claude Agent SDK adapters.
  - Sessions, context trimming and `agentkit chat` exist for the native adapter only. The ADK adapter takes a string and keeps no history.
- Documentation lives in `README.md` and `docs/` (start at `docs/index.md`), with `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md` and `CHANGELOG.md` at the root. The docs describe behaviour precisely (defaults, exit codes, which tools run in evals), so a change to that behaviour needs the matching page changed in the same commit. `tests/unit/test_docs.py` fails when a CLI command or option, a builtin tool or its argument, a configuration key or an environment variable is undocumented, when `docs/index.md` misses a page, or when a relative link or anchor breaks. The walk-through in `docs/getting-started.md` was verified by following it; re-run it after changing the CLI or the project template. Prose claims about "verified" and "not verified" (README status section, `docs/models.md`, `docs/adk.md`) must be updated when that changes, for example after a live run against a hosted provider.

Decisions made by the owner: name `agentkit`, Python, Apache-2.0, Anthropic first, LM Studio as the default local server, SearXNG for `web_search`, ADK as the only library adapter.

## Rules from the spec that shape every change

- Do not add features the spec does not list. Speculative abstractions go to the backlog (section 20), not the code.
- Thin over complete: prefer a function to a class, a class to a hierarchy; each module readable in one sitting.
- Model choice, limits, timeouts, retry policy and tool enablement live in config, never in prompts or agent code. Model IDs appear only in `config/models.yaml`.
- No module imports an agent library or provider SDK at top level outside its own adapter or provider file; SDKs are optional extras.
- `asyncio` throughout; sync wrappers only in `cli/`.
- Every kit error derives from `AgentKitError` and its message says what to do next.
- Unit tests use no network and no API keys (fake provider, fake tools). Real-provider tests carry the `integration` marker, local-server tests the `local_model` marker.
- Safe by default: a tool is side-effecting unless declared otherwise, side effects need approval unless config grants it, every run has a budget, tool output is untrusted data.

## Architecture

Dependency direction: `config` ← `models` ← `tools`/`guardrails`/`observability`/`memory` ← `runtime` ← `evals` ← `cli`. Agent code names a model by role and describes an agent as an `AgentSpec`; config maps roles to providers, and a `RuntimeAdapter` executes the spec. One run is one `RunContext` (`runtime/context.py`): config, run ID, budget, approval channel, checks, tracer and state store, each with a safe default.

### Configuration

`load_config` (`config/loader.py`) deep-merges five layers, later overriding earlier: shipped `config/default.yaml` + `config/models.yaml` and `config/pricing.yaml` (loaded as the `models` and `pricing` sections) → project `agentkit.yaml` → `agentkit.<env>.yaml` → `AGENTKIT_*` environment variables (`__` separates nesting levels, values parsed as YAML) → `overrides`/`env` arguments. Mappings merge; lists and scalars replace.

- The result is one Pydantic model (`config/schema.py`), `extra="forbid"` everywhere, so an unknown key from any layer, including a stray `AGENTKIT_*` variable, is a `ConfigError`.
- Schema fields mostly have no Python default on purpose: `config/default.yaml` is the single place defaults live. A new config key needs both a schema field and a default there.
- The shipped YAML and the project template sit in top-level `config/` and `templates/`. The wheel force-includes them as `agentkit/_shipped_config` and `agentkit/_templates`; the lookup functions check there first and fall back to the checkout.
- Relative paths in config (`observability.jsonl_path`, the three `evals.*_dir`, `filesystem.root`, `sql_query.database`) are made absolute against the project directory by `_anchor_paths` when the config loads, so a command behaves the same from inside the project and with `--project-dir`. A new path-valued setting must be added there. Prompt files and eval fixtures are relative to the project too.
- Secrets come from the environment, never from YAML. Any new variable the kit reads must be added to `.env.example`.

### Model layer

`get_model(role)` (`models/registry.py`) returns a `Model` (`models/model.py`): a fallback chain of `Link`s, each holding one provider instance bound to one model ID. `Model.generate` is where the shared behaviour lives, once, for every provider: catalogue defaults fill unset request fields, `check_request` rejects what the model's `Capabilities` lack, then timeout, retry with jittered backoff (only on `ModelError(retryable=True)`), fallback to the next link, and cost from the `pricing` config.

- Providers (`models/providers/`, one file each) only translate between the common types in `models/base.py` and their API, and convert every failure to `ModelError` with `retryable` set. They do no retrying of their own. The registry imports provider modules lazily.
- `openai_compat` speaks chat-completions over plain `httpx`; `local`, `openai` and `google` are presets over it. `anthropic` uses the official SDK.
- `local` asks the server for its model list before its first call, so a stopped server, a base URL without port or `/v1`, and an unloaded model ID each get their own message.
- Providers may have an async `ping()` (not part of the `ModelProvider` protocol), which `agentkit doctor` calls to check connectivity without spending money: Anthropic counts the tokens of one word, the OpenAI-style providers list models (and check the key and that the model ID is offered), local lists the server's models. Anything new that talks to a service should get one.
- Provider switch: `<AGENT_NAME>_MODEL_PROVIDER`, then `MODEL_PROVIDER`, then `models.provider`. The value `local` remaps every hosted role to the catalogue entry `local/default` (model ID from `LOCAL_MODEL_ID`) and drops that role's fallbacks, so data never leaves the machine implicitly.
- Two additions to the spec's types: `OpaquePart` carries provider-specific blocks (Anthropic thinking blocks) that must be sent back unchanged in a tool loop, and the `forced_tool_choice` capability exists because current Claude models reject a forced `tool_choice`.
- A model without native structured output gets `generate_json`: prompt for JSON, validate with `jsonschema`, retry up to three times. A reply that is a tool call passes through untouched.
- `Model.stream` falls back only before the first event and does not retry or time out.
- `Usage.input_tokens` is the total including cache reads; cache writes are priced at the plain input rate.

### Tools and the executor

`@tool` (`tools/base.py`) turns a function into a `Tool`: the input schema is a Pydantic model built from the type hints (extra arguments forbidden), the description is the docstring up to `Args:`, and each `Args:` entry becomes that argument's description. A parameter named `ctx` receives the `RunContext` and is hidden from the model. Sync functions run in a thread.

Every tool call, from any adapter, an eval or `agentkit tools call`, goes through `execute_tool` (`runtime/executor.py`), in the order of spec section 11.5: validate arguments, approval, budget, run with a timeout, convert any exception to a `ToolError`, serialise, truncate, output checks, and wrap in an `<untrusted_content>` block when the tool was declared with `untrusted_output=True`; all inside a `tool_call` trace span. It never raises for a tool failure. It does raise `BudgetExceeded` and `ApprovalPending`, because those stop the run.

- Inside a tool, fail with `raise ToolFailure(kind, message)`; the message is addressed to the model and says what to do next.
- Approval policy (`guardrails/policy.py`): per-tool `tools.approval.overrides`, then `by_side_effects`, then `default`. Shipped config allows `none` and `read_external`; everything else goes to the approval channel. A destructive tool is only ever allowed by its own override.
- Approval channels (`guardrails/approvals.py`): `cli`, `callback`, `auto_deny` (the `RunContext` default, so nothing side-effecting runs unattended), `auto_approve` (refused in prod). A denial comes back to the model as a `permission` error and the run continues. A channel that returns `None` suspends the run.
- A new builtin goes in `tools/builtin/`, is added to `BUILTIN_TOOLS`, and reads its own section of `tools.settings` through a small Pydantic model. File tools must take every path through `resolve_in_root`.
- `http_fetch` fetches nothing until `allowed_domains` is set. It resolves the host itself, refuses unless every address is public, then connects to that checked address, and checks every redirect hop the same way.
- MCP (`tools/mcp.py`, extra `agentkit[mcp]`, imported only when a server is configured): `ToolRegistry.from_config` loads every server under `tools.mcp_servers`, registering each remote tool as `<server>_<tool>`. Such a `Tool` carries the server's JSON Schema in `json_schema` (validated with `jsonschema` in the executor) instead of a typed `input_model`. Side effects come only from config and default to `write`; output is always untrusted. Connections are persistent and process-wide: `_CONNECTIONS` runs one daemon thread with its own event loop, and each server's session is opened, held and closed by a single task there (anyio requires the same task to enter and exit). Callers on any loop or thread reach it with `submit(...)`, awaiting through `asyncio.wrap_future` so that a tool timeout cancels the remote call too. This is what lets a connection outlive the caller's event loop; the CLI starts a new loop per chat turn. Connections are keyed by server name and config, so registries share them; a failed call drops the connection and the next call reconnects, without retrying the failed call itself (it may have had side effects). `keep_alive: false` closes after each call. Everything is closed by an `atexit` hook, by `close_mcp_connections()`, and after each test by `tests/conftest.py`. Listing tools blocks on the same connection, so it works from sync and async callers. The SDK is on 2.x, whose names differ from 1.x (`MCPServer`, `input_schema`, `streamable_http_client`).
- `agentkit.tools.builtin.http_fetch` as an attribute is the tool, not the module; tests that patch module functions get the module from `sys.modules`.

### Runtime

`NativeAdapter` (`runtime/adapters/native.py`) is the reference loop and the default for tests and evals. `_loop` is a single async generator of `RunEvent`s; `run`, `stream` and `resume` all consume it. Each turn: budget check, model call in a `model_call` span, then either a final answer (final-answer checks, `output_schema` validation) or the tool calls, each through `execute_tool`.

- Stop reasons: `completed`, `budget_exceeded` (the error names the limit; the last assistant text is returned as partial output), `stalled` (the same call more than `guardrails.max_identical_tool_calls` times, or `max_empty_steps` steps with neither a tool call nor text), `error`, `awaiting_approval`.
- Resumable approvals: `awaiting_approval` carries a `PendingApproval` (plain data: transcript, earlier results of the turn, counters). `adapter.resume(spec, pending, decision, ctx)` continues from that tool call.
- A successful call to a tool named `finish` ends the run with its `result` (or `summary`) as the output.
- Hand-off: an agent with `sub_agents` gets the `handoff` tool automatically, its description listing those agents. A successful `handoff` runs the named agent as a nested run (child `run` span, same budget) on the handed-over message only, and that agent's result becomes the result of the whole run. The adapter is given the project's agents through `get_adapter(..., agents=...)`.
- Setup mistakes (unknown tool name, an undefined sub-agent, a model without tool calling for an agent with tools, a missing prompt file) raise `AgentKitError` instead of returning a result.
- `load_project` (`runtime/project.py`) imports a project's `tools/*.py` and `agents/*.py` as `tools.x` / `agents.x`, collecting `Tool`s, `AgentSpec`s, `Workflow`s and Pydantic models. It first drops those names, and every module imported from the previously loaded project, from `sys.modules`, so one process can load several projects, one at a time.
- `AgentSpec.instructions` is prompt text or a path to a prompt file under the project; `{{name}}` placeholders are filled from `AgentSpec.variables`.

### Sessions and context

`memory/` holds two stores, each with an in-memory and a SQLite implementation chosen by `memory.store` (`make_stores`): `StateStore` (key-value, what `remember`/`recall` use through `ctx.state`) and `SessionStore` (conversation history).

- A conversation is a sequence of ordinary runs. `stream_in_session` (`runtime/session.py`) loads the history, runs the adapter on history plus the new user message, and appends the turn. A turn that does not complete is stored as the user message and a one-line note, so a half-finished tool round never enters the history. `agentkit chat` is a loop around it.
- `storable` decides what is saved: no system messages (each run adds its own prompt, and a hand-off's sub-agent prompt must not leak into later turns) and no `OpaquePart`s.
- Context trimming (`memory/context.py`): before every model call the native loop asks `ContextManager.view` for the messages to send. While the estimated size is under `memory.context.trigger_ratio` of the model's declared `context_window`, that is the transcript itself. Beyond it, the view is a copy: `drop_tool_results` blanks the oldest tool results, `summarize` replaces everything before a recent user message with a summary from the `fast` role (cached, extended as the conversation grows, counted in the budget but not as a step). `RunResult.messages` and the session store always hold the full transcript.
- Thinking blocks and edited history: Claude rejects a replayed thinking block when the history before it has changed. So any trimmed view has every `OpaquePart` removed, and none are stored across turns. Inside one untrimmed run they are replayed unchanged, as the API requires. Keep that rule if you change either place.
- The size estimate is four characters per token (`estimate_tokens`), not a provider count.

### Patterns

`patterns/` holds the shapes that are more than one `AgentSpec`. `router(...)` returns an `AgentSpec` on the `fast` role whose only job is to `handoff`. The others subclass `Pattern` (`patterns/base.py`), a Pydantic model with a `name`, a `description` and an async `run(input, adapter, project, ctx)`:

| Class | Shape |
|---|---|
| `Workflow` | Agents in a fixed order, each output feeding the next. |
| `PlanAndExecute` | A planner returns a `Plan`; an executor runs each step with the task and earlier results. |
| `FanOut` | Workers run concurrently on the same input; a merger gets all results. |
| `ReviseLoop` | A writer drafts, an evaluator returns a `Verdict`, bounded by `max_rounds`. |

- A project defines patterns next to its agents; `load_project` recognises them by the `agentkit_pattern` class attribute (it cannot import `patterns`, which imports it). `run_named(project, adapter, name, input, ctx)` runs a pattern or an agent, and is what `agentkit run` and the eval runner call.
- `run_named` owns what is common: the pattern's `run` span, collecting every agent's transcript, and restating usage and steps for the whole pattern. A pattern's `run` only decides which agent runs when, on what input, and returns the `RunResult` that should stand for the run. A step that does not complete ends the pattern with that step's result.
- All agents in a pattern share `ctx`, so one budget bounds the pattern. `RunResult.steps` of a nested run is therefore the running total, not that agent's own count.
- Where a pattern needs structured output from an agent (`Plan`, `Verdict`) it copies the spec with `output_schema` set, so project authors do not.
- `FanOut` gives each worker `dataclasses.replace(ctx, tracer=ctx.tracer.child())`: a tracer that writes to the same trace but tracks its own span nesting. Anything else that runs agents concurrently must do the same, or spans get the wrong parents.
- With a scripted eval model, concurrent workers that call no tools consume replies in the order they are listed.
- Human in the loop needs no code: it is the approval gate plus the `ask_human` tool.

### ADK adapter

`AdkAdapter` (`runtime/adapters/adk.py`, extra `agentkit[adk]`, imported only when `runtime.adapter` is `adk`) builds an ADK `LlmAgent` per run; `to_native(spec)` returns it. Each kit tool becomes a `KitTool` whose `run_async` calls `execute_tool`, so approvals, budgets, timeouts and tracing are the kit's. Budget checks, usage, cost and the `model_call` span hang on ADK's before/after model callbacks.

- Models: `google` entries are passed to ADK by ID, `anthropic` becomes `LiteLlm("anthropic/<id>")`, and local or other OpenAI-style endpoints become `LiteLlm("openai/<id>", api_base=...)`. The fake provider, and any model an eval injects (scripts, record, replay), run through `KitLlm`, an ADK model backed by the kit's model layer; that is what lets the same eval suite pass on both adapters.
- The adapter's known gaps are listed in its module docstring: string input only, no resumable approvals, no `sub_agents`, the kit's retry/timeout/fallback do not apply to model calls ADK makes itself, and an unknown tool call fails the run.
- `google-adk` caps `opentelemetry-api`, so keep the kit's own requirement on it loose. An over-tight pin once made `uv` silently resolve an old ADK with known vulnerabilities; `pip-audit` in CI is what catches that.

### Observability

`Tracer` (`observability/tracer.py`) writes one record per span when the span ends: by default to `.agentkit/traces/<run_id>.jsonl`, and always to the OpenTelemetry API (a no-op unless the application configures an SDK). Records carry a `seq` number because a parent span is written after its children; `read_trace` sorts by it. Before export every string is redacted with `guardrails.redact_patterns` and the values of secret-looking environment variables. Inputs and outputs go through `span.set_content` and are dropped when `capture_content` is false; `span.set` is for metadata.

Logs (`observability/logs.py`): `configure_logging(cfg)` installs one stderr handler on the `agentkit` logger (the CLI calls it when it loads the config; library users call it themselves) writing JSON lines, redacted the same way as traces, at `observability.log_level`. Run code must log through `ctx.logger`, a `LoggerAdapter` that stamps `run_id` on every record; that is what joins a log line to `agentkit trace <run_id>`. Do not use a contextvar for this (reset fails across async generators). A completed run logs at DEBUG, a stopped or failed one and every failed tool call at WARNING/ERROR (`RunContext.log_result`), so the default INFO output is quiet.

Redaction has two layers. Traces and logs are always redacted. `Checks.from_config` (called by `RunContext` when no `checks=` is given) also adds the `Redactor` at each point in `guardrails.redact_at` (default tool output and final answer), so the model neither sees nor repeats what it removes. The user's input is left alone by default. Passing `checks=` to `RunContext` replaces the configured checks entirely. The names allowed in `redact_patterns` are in `guardrails/patterns.py`, a module of its own because both the config schema and the checks need them and would otherwise import each other.

### Evals

A suite (`evals/dataset.py`) is a YAML or JSONL file naming an agent and its cases. `expect:` is sugar that becomes graders; `Suite.graders_for` is the single place that order is defined. `run_suites` (`evals/runner.py`) runs each case through the configured adapter (`get_adapter`) and `run_named`, with:

- Tools from `eval_registry` (`evals/fakes.py`): unless `--live`, a tool with a fixture answers from it, a side-effect-free tool runs for real, and any other tool reports a missing fixture. Fixtures live in `fixtures/` next to the suite.
- Approvals from `EvalApprovals`: everything side-effecting is denied unless the case lists it in `setup.approve_tools`.
- A model chosen in this order: `--replay`/`--record` (cassette in `evals.cassette_dir`, via `get_model(provider_factory=...)`), else the case's `setup.fake_model` script (one script per attempt, shared by every agent in it, so a hand-off or workflow continues the same script), else the configured model. Replay never builds the real provider and uses the capabilities stored in the cassette, so its requests hash the same as when recorded.

Graders (`evals/graders.py`) are async functions in the `GRADERS` dict. `llm_judge` uses the `judge` role, structured output, and the versioned prompt in `evals/prompts/`; it is skipped, not failed, when the judge is the fake provider. Results are saved as JSON under `evals.results_dir`; run IDs sort by time, which is what makes `latest` work. `eval run` exits 1 below `evals.fail_under`, `eval compare` beyond `evals.max_pass_rate_drop`.

The example projects under `examples/` and the template under `templates/agent/` ship suites whose model replies are scripted, which is how they pass in CI and right after `agentkit new` with no key.

## Releases

`CHANGELOG.md` defines the public contract (`AgentSpec` and the patterns, the tool contract, the config schema, the eval format) and what breaks it. A change to any of them, including a changed default, needs a migration note there under its release, and a major version once past 1.0. `tests/unit/test_changelog.py` keeps the changelog's top release equal to `agentkit.__version__` and to `pyproject.toml`; bump all three together.

## Gotchas

- `ruff format` rewrites Python code blocks inside Markdown. `*.md` is excluded in `pyproject.toml` so the spec's snippets stay as written; keep that exclusion.
- Tests run in a temporary working directory (`tests/conftest.py`), so anything written relative to it never lands in the repository.
- Template files use `__AGENT_NAME__` and `__PROJECT_NAME__` as placeholders, not `{{...}}`, which is the prompt-variable syntax. `templates/router` and `templates/workflow` are overlays copied over `templates/agent`; they mirror `examples/router` and `examples/workflow`, so change both.
