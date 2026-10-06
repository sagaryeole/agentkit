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
uv run agentkit config validate | show    # --env <name>, --project-dir <dir>
uv run agentkit doctor
uv run agentkit models list [--local]     # role -> model; --local asks the local server for its IDs
uv run agentkit models test <role>        # one real call: reply, tokens, cost, latency
uv run agentkit tools list | describe <tool> | call <tool> --args '{"expression": "6*7"}'
uv run python examples/single_agent/agent.py "What is 17 times 23?"

# opt-in live tests (skipped when the server or key is absent)
MODEL_PROVIDER=local LOCAL_MODEL_ID=<id> uv run pytest tests/integration -m local_model
uv run pytest tests/integration -m integration        # needs ANTHROPIC_API_KEY
```

CI (`.github/workflows/ci.yml`) runs ruff, mypy, pytest and a `pip-audit` scan of the locked dependencies.

## Build status

Milestones are built strictly in order; a milestone does not start until the previous one meets its acceptance criteria in spec section 17.

- **M1 skeleton and config: done.** `config/`, `cli/`, `errors.py`.
- **M2 model layer: done**, except that the Anthropic provider has never been run against the real API (no credentials were available); its conversion and error mapping are unit-tested only.
- **M3 tools: done.** `@tool`, `ToolRegistry`, the tool executor, the approval policy resolver, and five builtins (`calculator`, `current_time`, `read_file`, `list_files`, `http_fetch`). Not yet built from section 9: the other builtin tools (M7), MCP loading, and `docs/writing-tools.md` (M9).
- **M4 native runtime and guardrails: next.** The packages `memory`, `observability`, `evals`, `patterns` are docstring-only placeholders; `runtime` has only `RunContext` and the executor, `guardrails` only the policy resolver. `templates/` and `docs/` do not exist yet. `examples/single_agent/agent.py` is a hand-written loop on the model layer and tool executor, to be replaced by an `AgentSpec` on the native runtime in M4.

Decisions already made by the owner: name `agentkit`, Python, Apache-2.0, Anthropic as the first hosted provider, LM Studio as the default local server. Still open (spec section 21, ask before building the part that depends on it): `web_search` backend, `run_python` sandbox, trace backend beyond JSONL, whether LangGraph and Claude Agent SDK adapters are wanted.

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

The intended dependency direction is: `config` ← `models` ← `tools`/`guardrails` ← `runtime` ← `evals`/`patterns`/`cli`. Agent code names a model by role (`default`, `fast`, `reasoning`, `judge`) and an `AgentSpec` declaratively; config maps roles to providers, and a `RuntimeAdapter` (native, ADK, ...) executes the spec. All adapters must route tool calls through the single tool executor so approvals, budgets, timeouts and tracing apply regardless of the library.

### Configuration (implemented)

`load_config` in `src/agentkit/config/loader.py` deep-merges five layers, later overriding earlier: shipped `config/default.yaml` + `config/models.yaml` and `config/pricing.yaml` (loaded as the `models` and `pricing` sections) → project `agentkit.yaml` → `agentkit.<env>.yaml` → `AGENTKIT_*` environment variables (`__` separates nesting levels, values parsed as YAML) → `overrides`/`env` arguments. Mappings merge; lists and scalars replace.

- The result is validated as one Pydantic model (`config/schema.py`), `extra="forbid"` everywhere, so an unknown key from any layer, including a stray `AGENTKIT_*` variable, is a `ConfigError`.
- Schema fields mostly have no Python default on purpose: `config/default.yaml` is the single place defaults live. A new config key needs both a schema field and a default there.
- Cross-field rules live in model validators: roles and fallbacks must reference keys in `models.models`, `evals.judge_role` must be a role, `auto_approve` is refused when `env` is `prod`.
- The shipped YAML sits in the top-level `config/` directory, not inside the package. The wheel force-includes it as `agentkit/_shipped_config`; `shipped_config_dir()` checks that location first and falls back to the checkout's `config/`.
- `config/pricing.yaml` is loaded as the `pricing` section; keys are model keys or `<provider>/*`.
- Secrets come from a `SecretProvider` (`config/secrets.py`, env by default) and never from YAML. Any new variable the kit reads must be added to `.env.example`.

### Model layer (implemented)

`get_model(role)` in `models/registry.py` returns a `Model` (`models/model.py`): a fallback chain of `Link`s, each holding one provider instance bound to one model ID. `Model.generate` is where the shared behaviour lives, once, for every provider: catalogue defaults fill unset request fields, `check_request` rejects what the model's `Capabilities` lack, then timeout, retry with jittered backoff (only on `ModelError(retryable=True)`), fallback to the next link, and cost from the `pricing` config. Budget checks (M4) and tracing spans (M5) belong in the same place.

- Providers (`models/providers/`, one file each) only translate between the common types in `models/base.py` and their API, and convert every failure to `ModelError` with `retryable` set. They do no retrying of their own (the Anthropic SDK's retries are switched off). The registry imports provider modules lazily, so a missing optional SDK only matters when that provider is used.
- `local` is a preset over `openai_compat` (plain `httpx`, no SDK). Before its first call it asks the server for its model list so that a stopped server, a base URL without port or `/v1`, and an unloaded model ID each get their own message.
- Provider switch: `<AGENT_NAME>_MODEL_PROVIDER`, then `MODEL_PROVIDER`, then `models.provider`. The value `local` remaps every hosted role to the catalogue entry `local/default` (model ID from `LOCAL_MODEL_ID`) and drops that role's fallbacks, so data never leaves the machine implicitly; any other value leaves roles as mapped.
- Two additions to the spec's types, both needed in practice: `OpaquePart` carries provider-specific blocks (Anthropic thinking blocks) that must be sent back unchanged in a tool loop, and the `forced_tool_choice` capability exists because current Claude models reject a forced `tool_choice`.
- A model without native structured output gets `generate_json`: prompt for JSON, validate with `jsonschema`, retry up to three times.
- `Model.stream` falls back only before the first event and does not retry or time out.
- `Usage.input_tokens` is the total including cache reads; cache writes are priced at the plain input rate.
- Tests drive all of this through `FakeProvider` (scripted replies, exceptions or callables) and `httpx.MockTransport`; pass `sleep=` to `Model` to record backoff instead of waiting.

### Tools (implemented)

`@tool` (`tools/base.py`) turns a function into a `Tool`: the input schema is a Pydantic model built from the type hints (extra arguments forbidden), the description is the docstring up to `Args:`, and each `Args:` entry becomes that argument's schema description. A parameter named `ctx` receives the `RunContext` (`runtime/context.py`) and is hidden from the model. Sync tool functions are fine; they run in a thread.

Every tool call, from any adapter or from `agentkit tools call`, goes through `execute_tool` (`runtime/executor.py`), which never raises: validate arguments, resolve the approval policy, run with a timeout, convert any exception to a `ToolError`, serialise, truncate to `tools.max_output_chars`, and wrap the content in an `<untrusted_content>` block when the tool was declared with `untrusted_output=True`. Budget checks, the real approval channels, output checks (M4) and trace spans (M5) slot into this function in the order given in spec section 11.5.

- Inside a tool, fail by raising `ToolFailure(kind, message)`; the message is addressed to the model and says what to do next. Anything else that escapes becomes kind `internal`.
- Policy (`guardrails/policy.py`): per-tool `tools.approval.overrides`, then `tools.approval.by_side_effects`, then `tools.approval.default`. Shipped config allows `none` and `read_external` and requires approval for the rest. Until M4 adds approval channels, `require` is answered with a `permission` error, so write tools only run with an explicit `allow` override.
- A new builtin goes in `tools/builtin/`, is added to `BUILTIN_TOOLS`, and reads its own section of `tools.settings` through a small Pydantic model (see `FilesystemSettings`, `HttpFetchSettings`).
- File tools must take every path through `resolve_in_root`. `tools.settings.filesystem.root` is relative to the current working directory, not to `--project-dir`.
- `http_fetch` fetches nothing until `allowed_domains` is set. It resolves the host itself, refuses unless every address is public, then connects to that checked address (with the original `Host` header and TLS server name) so a second DNS answer cannot redirect it; each redirect hop is checked the same way. Tests replace `_resolve` and `_client` in that module; note that `agentkit.tools.builtin.http_fetch` as an attribute is the tool, so get the module from `sys.modules`.

## Gotchas

- `ruff format` rewrites Python code blocks inside Markdown. `*.md` is excluded in `pyproject.toml` so the spec's snippets stay as written; keep that exclusion.
