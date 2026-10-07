# Development guide

This is for people changing agentkit itself. For using it, start with [Getting started](getting-started.md). For how to propose a change, read [CONTRIBUTING.md](../CONTRIBUTING.md) first.

## Set up

```bash
git clone https://github.com/sagaryeole/agentkit agentkit
cd agentkit
uv sync                        # installs the kit and every development dependency, extras included
uv run pytest                  # the whole test suite; it needs no network and no keys
```

`uv sync` installs the optional extras (`anthropic`, `adk`, `mcp`) as development dependencies, so the whole suite can run. A change must pass this before it is merged:

```bash
uv run ruff check . && uv run ruff format --check .      # lint and formatting
uv run mypy                                              # strict type checking of src/agentkit
uv run pytest                                            # tests
```

CI runs these, the smoke evals of every example project (and of one example again on the ADK adapter), and an audit of the locked dependencies for known vulnerabilities (`pip-audit`).

## Repository layout

```
agentkit/
├── src/agentkit/
│   ├── config/          schema, layered loader, secrets
│   ├── models/          common types, Model wrapper, registry
│   │   └── providers/   one file per provider: anthropic, openai, google, openai_compat, local, fake
│   ├── tools/           @tool, registry, MCP loader
│   │   └── builtin/     the ready-made tools
│   ├── guardrails/      budget, approvals, checks, policy, redaction patterns
│   ├── runtime/         run context, tool executor, specs, sessions, project loader
│   │   └── adapters/    native loop, Google ADK
│   ├── memory/          session and state stores, context trimming
│   ├── observability/   tracer, structured logs
│   ├── evals/           dataset, graders, fixtures, cassettes, runner, reports
│   ├── patterns/        router, workflow, planner, fan-out, reviser loop
│   └── cli/             the agentkit command, project scaffolding
├── config/              shipped defaults: default.yaml, models.yaml, pricing.yaml
├── templates/           what `agentkit new` copies
├── examples/            eight complete projects, each with an eval suite
├── tests/
│   ├── unit/            no network, no keys
│   └── integration/     opt-in, against real servers
├── docs/                this documentation
└── .github/             CI workflow, issue and pull request templates
```

Packages depend on each other in one direction: `config` ← `models` ← `tools`/`guardrails`/`observability`/`memory` ← `runtime` ← `evals` ← `cli`. A package imports only from those on its left. [Concepts](concepts.md) explains what each does. `CLAUDE.md` at the root holds the same architecture notes in a denser form, written for AI coding assistants, and is useful to humans too.

`AGENTIC_FRAMEWORK_SPEC.md` is the specification the kit was built from. It records the decisions behind the design, including what was left out on purpose.

## Rules that shape every change

These come from the specification and are checked in review.

- **No features that are not wanted.** Speculative abstractions go to the backlog, not the code. An abstraction is added when two real agents need the same thing.
- **Thin over complete.** Each module should be readable in one sitting. Prefer a function to a class and a class to a hierarchy.
- **Configuration over code.** Model choice, limits, timeouts, retry and tool enablement live in config. Model IDs are written only in `config/models.yaml`.
- **No agent library or provider SDK is imported at module level outside its own provider or adapter file.** They are optional extras.
- **`asyncio` throughout.** Synchronous wrappers live in `cli/` and nowhere else.
- **Every error derives from `AgentKitError` and says what to do next.** Messages are written for the person reading them, naming the cause and the fix.
- **Unit tests need no network and no keys.** They use the fake provider, fake tools and `httpx.MockTransport`. Tests against a real provider carry the `integration` marker, and tests needing a local model server carry `local_model`; both skip themselves when the service is absent.
- **Safe by default.** A tool is side-effecting unless declared otherwise, side effects need approval, every run has a budget, and tool output is untrusted data.

## Testing

```bash
uv run pytest                                               # everything, offline
uv run pytest tests/unit/test_config.py::test_unknown_key_fails   # one test
uv run pytest --cov=agentkit --cov-report=term              # with coverage
```

The target is at least 85% line coverage on `models`, `tools`, `guardrails`, `runtime` and `evals`; coverage is well above that today.

Opt-in tests against real services:

```bash
MODEL_PROVIDER=local LOCAL_MODEL_ID=<id> uv run pytest tests/integration -m local_model
ANTHROPIC_API_KEY=... uv run pytest tests/integration -m integration
```

Useful things to know when writing tests:

- Tests run in a temporary working directory (`tests/conftest.py`), so traces, eval results and memory files never land in the repository.
- `tests/unit/helpers.py` has builders for scripted agents: `scripted_adapter(script, *tools)` returns a native adapter whose model replays a script, and `tool_calls(...)` builds a model turn that asks for tools.
- `FakeProvider` takes a list of replies, exceptions or functions of the request. `Model(..., sleep=...)` records backoff instead of waiting.
- MCP tests start a real MCP server as a subprocess, and `tests/conftest.py` closes any server a test left running.
- Docs are tested too. `tests/unit/test_docs.py` fails if a CLI command or option, a builtin tool, a configuration key or an environment variable is missing from the documentation, or if a link in the documentation is broken.

## Extending the kit

### Adding a provider

1. Create `src/agentkit/models/providers/<name>.py` with a class that has `name`, `capabilities`, and async `generate`, `stream` and `count_tokens`. Optionally add `async def ping(self) -> str` for `agentkit doctor`.
2. Translate between the kit's types and the API. Convert every failure to `ModelError`, with `retryable` set correctly: true for rate limits, timeouts and 5xx, false for validation errors. Do not retry inside the provider.
3. If it needs an SDK, import it only in that file, and add an optional extra to `pyproject.toml`.
4. Register it in `build_provider` in `models/registry.py`, with a clear `ModelError` if the SDK is missing.
5. Test the translation and the error mapping with stubs or `httpx.MockTransport`; see `tests/unit/test_anthropic_provider.py` and `test_local_provider.py`.
6. Document it in [Models and providers](models.md) and add a line to the changelog.

For a service that speaks the OpenAI protocol, subclass `OpenAICompatProvider` as `openai.py` and `google.py` do. That is usually all that is needed.

### Adding a builtin tool

1. Add it under `src/agentkit/tools/builtin/`, with `@tool`, an honest `side_effects`, a docstring written for the model, and `untrusted_output=True` if it returns outside content.
2. Read configuration from its own section of `tools.settings` through a small Pydantic model with `extra="forbid"`, and add that section to `config/default.yaml`.
3. Confine what it can reach in code. File tools must take every path through `resolve_in_root`.
4. Add it to `BUILTIN_TOOLS` in `tools/builtin/__init__.py`.
5. Test it through `execute_tool`, including the failures and the safety rules. Add at least one eval case that uses it.
6. Add it to [the builtin tools reference](reference/builtin-tools.md). A test fails until you do.

### Adding a grader

Graders are async functions in `src/agentkit/evals/graders.py`, registered in the `GRADERS` dictionary, with the signature `(case, result, params, ctx) -> GradeResult`. Raise `AgentKitError` for a case that is badly written, and return a failing `GradeResult` for an agent that did not meet it. Document its parameters in [Writing evals](writing-evals.md).

### Adding a pattern

Subclass `Pattern` in `src/agentkit/patterns/`, implement `async run(input, adapter, project, ctx)`, and return the `RunResult` that should stand for the run. `run_named` already opens the pattern's run span, collects the transcripts and restates the totals, so a pattern only decides which agent runs when. If agents run concurrently, give each `dataclasses.replace(ctx, tracer=ctx.tracer.child())`. Add an example project with an eval suite, and a section in [Patterns](patterns.md).

### Adding a configuration key

A key needs a field in `src/agentkit/config/schema.py` and a default in `config/default.yaml`; the schema deliberately has few Python defaults, so the YAML is the one place defaults live. If it holds a path, make it absolute in `_anchor_paths` in `config/loader.py`. Document it in [Configuration](configuration.md); a test fails until you do. Changing the meaning or default of an existing key is a breaking change: see [Changelog](../CHANGELOG.md).

### Adding a command

Commands are in `src/agentkit/cli/main.py`. Take `--env` and `--project-dir` if the command reads configuration, load it with `_load_or_exit`, convert `AgentKitError` to a message on stderr and exit code 1, and document it in the [CLI reference](reference/cli.md). A test fails until you do.

### Adding a runtime adapter

Implement `run`, `stream` and `to_native`, and route every tool call through `execute_tool`, so approvals, budgets, timeouts and tracing apply whatever the library does. Record any gap against the native adapter in the adapter's docstring and in its [documentation page](adk.md). `runtime/adapters/adk.py` is the worked example. Register it in `get_adapter`.

## Documentation

The documentation is part of the product, and some of it is checked by tests. When you change behaviour that a page describes (a default, an exit code, what a tool refuses), change the page in the same change.

- Write for the reader who has not seen the code. Say what happens, then why.
- Check examples by running them. Commands and code in the docs should work as written.
- State what is not verified or not built, rather than leaving it implied.
- Relative links only, so the docs work in a checkout and on the hosting site.

## Releases

1. Move the changelog entries to a new version heading in `CHANGELOG.md`, with a migration note for any breaking change to `AgentSpec` and the patterns, the tool contract, the configuration schema or the eval format.
2. Bump the version in `pyproject.toml` and `src/agentkit/__init__.py`, and the changelog's top heading. A test fails if the three differ.
3. Run the full checks, and build the package: `uv build`.
4. Tag the commit `v<version>`.

Semantic versioning applies. Until 1.0, a breaking change goes in a minor version and still gets a migration note.
