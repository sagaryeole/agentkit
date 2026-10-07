# Changelog

All notable changes are recorded here. The project follows [semantic versioning](https://semver.org).

## Versioning and migration notes

Four things are the public contract. A change that breaks code or files written against them needs a new major version and a migration note in this file, under the release that made the change:

| Contract | What counts as breaking |
|---|---|
| `AgentSpec` (and the pattern classes) | Removing or renaming a field, changing what a field means, or making an optional field required. |
| The tool contract (`@tool`, `ToolFailure`, `RunContext` as tools see it) | Changing how the schema or description is derived, what `side_effects` allows, or the attributes of `ctx`. |
| The configuration schema | Removing or renaming a key, changing a key's meaning, or changing a default so that existing projects behave differently. Unknown keys are errors, so a removed key breaks the projects that set it. |
| The eval dataset format and its saved results | Changing what a key means, removing a grader or a grader parameter, or changing the result JSON so older runs cannot be read by `eval report` and `eval compare`. |

Adding an optional field, key, grader or tool is not breaking. Behaviour that is described in `docs/` counts as part of the contract it belongs to.

A migration note says what changed, who is affected, and the exact edit to make. Use this shape:

```
### Migration: <what changed>
Affects: <who>
Before: <old config, code or file>
After:  <new config, code or file>
```

Before 1.0, breaking changes go in a minor version, as semantic versioning allows, and still get a note.

## 0.1.0 (unreleased)

The first version. Everything below is new. Anyone who has been running from the `main` branch before this release should read the migration notes at the end of this section.

### Added

- **Configuration.** Layered YAML plus `AGENTKIT_*` environment variables, validated as one model; unknown keys are errors. `agentkit config show|validate`.
- **Model layer.** Roles (`default`, `fast`, `reasoning`, `judge`) mapped to providers by configuration, with retry, timeout, fallback chains and cost tracking. Providers: Anthropic, OpenAI, Gemini (through its OpenAI-compatible endpoint), any OpenAI-compatible endpoint, local models (LM Studio, Ollama), and a scripted fake. `MODEL_PROVIDER=local` moves every role to a local model. `agentkit models list|test`.
- **Tools.** The `@tool` decorator, a registry, and one executor that validates, applies approval policy, enforces timeouts, truncates and delimits untrusted output. Builtins: `calculator`, `current_time`, `read_file`, `list_files`, `search_files`, `write_file`, `http_fetch`, `web_search` (SearXNG), `sql_query`, `ask_human`, `remember`, `recall`, `finish`, `handoff`. Tools from MCP servers, over stdio or HTTP, with persistent connections. `agentkit tools list|describe|call`.
- **Guardrails.** Per-run budgets, loop and stall detection, an approval gate with `cli`, `callback`, `auto_deny` and `auto_approve` channels and resumable runs, and input and output checks.
- **Runtime.** `AgentSpec`, a native tool-calling loop, and a Google ADK adapter. `agentkit run`.
- **Patterns.** Single agent, fixed workflow, router, hand-off, planner and executor, parallel fan-out and merge, evaluator and reviser loop, and human in the loop. Examples for each under `examples/`.
- **Sessions.** Conversation history, context trimming (blank old tool results, or summarise older turns), and `agentkit chat`.
- **Observability.** A JSON-lines trace per run, spans mirrored to the OpenTelemetry API, structured logs that carry the run ID, redaction of secrets and personal data, and `agentkit trace`.
- **Evals.** A dataset format, eleven graders, fixture-backed fake tools, record and replay, reports, run comparison and CI thresholds. `agentkit eval run|report|compare`.
- **Scaffolding.** `agentkit new` with `single`, `router` and `workflow` templates that pass their starter evals with no API key.
- **Diagnostics.** `agentkit doctor` checks configuration and credentials and contacts each model in use without generating anything.
- **Documentation.** README, `docs/patterns.md`, `docs/writing-tools.md`, `docs/writing-evals.md`, `SECURITY.md`.

### Known limitations

- The Anthropic, OpenAI and Gemini providers, `doctor`'s checks of them, and context trimming against a Claude model have been tested with stubs and mocks only, not against the live services. The local-model path has been run for real.
- Sessions, trimming and hand-offs work on the native adapter only. On the ADK adapter, input is a single string, approvals cannot be resumed, and the kit's retry and fallback do not apply to model calls.
- The MCP HTTP transport has not been run; stdio has.
- Not provided: a tool that executes code, an OTLP trace exporter, LangGraph and Claude Agent SDK adapters, and a secret provider for cloud secret managers (the interface exists; only environment variables are implemented).

### Migration notes (for users of the pre-release `main` branch)

### Migration: relative paths in the config are relative to the project
Affects: anyone who ran commands with `--project-dir` from another directory.
Before: `observability.jsonl_path`, `evals.dataset_dir`, `evals.results_dir`, `evals.cassette_dir`, `tools.settings.filesystem.root` and `tools.settings.sql_query.database` were resolved against the current directory.
After: they are resolved against the project directory (the one holding `agentkit.yaml`). Move any traces, results, cassettes or workspace you created in the wrong place, or set an absolute path.

### Migration: the redactor now runs on tool output and the final answer
Affects: agents whose tools return email addresses or API-key-shaped strings that the model needs to see.
Before: `guardrails.redact_patterns` applied to traces only.
After: it also applies to tool output and the final answer, and to logs. Restore the old behaviour for the model with `guardrails.redact_at: []`; add `user_input` to redact what the user types as well.

### Migration: `agentkit tools` commands load the project
Affects: scripts that relied on `tools list` showing only builtin tools.
Before: only the enabled builtin tools were listed.
After: the project's own tools in `tools/` and the tools of configured MCP servers are listed too, and configured MCP servers are started to find them.

### Migration: workflows are one kind of pattern
Affects: code that used `project.workflows` or `run_workflow`.
Before: `Project.workflows`, and `run_workflow(workflow, input, adapter, project, ctx)`.
After: `Project.patterns` holds every pattern, and `run_named(project, adapter, name, input, ctx)` runs a pattern or an agent. Define workflows exactly as before; only code that called those two names directly changes.

### Migration: the example agent is a project
Affects: anyone who ran `examples/single_agent/agent.py`.
Before: a Python script.
After: a project; run it with `agentkit run single_agent "..." --project-dir examples/single_agent`.
