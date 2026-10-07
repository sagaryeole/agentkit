# Troubleshooting and FAQ

Every error the kit raises says what to do next. This page collects the ones you are most likely to meet, with the cause and the fix. When something is not listed, `uv run agentkit doctor` and `uv run agentkit trace <run id>` find most problems.

## First steps for any problem

1. **`uv run agentkit doctor`** checks configuration, credentials, and that each model in use is reachable.
2. **`uv run agentkit config validate`** names any bad configuration key. `config show` prints what the kit is actually using.
3. **`uv run agentkit trace <run id>`** shows what a run did. The run ID is at the end of the summary line, and in every log line about the run.
4. **Set `observability.log_level: DEBUG`** to log the start and end of every run.

## Configuration

### `Invalid configuration: ... Extra inputs are not permitted`

A key is misspelled or does not exist. The message names its path, for example `limits.max_stepz`. Unknown keys are errors on purpose, so a typo cannot silently do nothing. A stray `AGENTKIT_*` environment variable counts too: `AGENTKIT_LIMITS__NOPE=1` is rejected. Check the spelling against the [configuration reference](configuration.md#reference).

### My `.env` file is ignored

The kit does not read `.env`. Export the variables in your shell, or run through uv: `uv run --env-file .env agentkit ...`.

### My settings seem to have no effect

- Run `agentkit config show --project-dir <dir>` to see the merged result. Check that you are editing the `agentkit.yaml` in the project directory you are using.
- A later source overrides an earlier one. An `AGENTKIT_*` environment variable beats `agentkit.yaml`, and `agentkit.<env>.yaml` beats both of the files before it. `--env` or `AGENTKIT_ENV` chooses which `<env>` file is read.
- Lists replace rather than extend. Setting `tools.enabled` to `[calculator]` removes every other tool from the list.

### Files and traces end up in the wrong place

Relative paths in the configuration are relative to the project directory, not to where you ran the command. Use `--project-dir`, or run from the project.

## Models

### `No lmstudio model server is answering at http://127.0.0.1:1234/v1`

The local server is not running. Start it from LM Studio's Developer tab or with `lms server start`, or `ollama serve` for Ollama. If it listens elsewhere, set `LOCAL_API_BASE`. For Ollama, also set `models.local.server: ollama`, or the kit looks at LM Studio's port.

### `The local base URL ... must include the port and end in '/v1'`

`LOCAL_API_BASE` or `models.local.api_base` is incomplete. Use a full URL such as `http://127.0.0.1:1234/v1`.

### `Model 'x' is not available on the lmstudio server ... Available: ...`

The model is not loaded, or the ID is wrong. Set `LOCAL_MODEL_ID` to one of the IDs listed, exactly as shown. `agentkit models list --local` prints them.

### `No local model is selected`

You switched to a local model (`MODEL_PROVIDER=local`) without saying which. Set `LOCAL_MODEL_ID`.

### `Model '...' needs the Anthropic SDK`

Install the extra: `uv sync --extra anthropic` (or `uv add 'agentkit[anthropic]'` in your own project). The same goes for `adk` and `mcp`.

### `Could not resolve authentication method` or `rejected the credentials`

No Anthropic credentials were found, or they were refused. Export `ANTHROPIC_API_KEY`. For other providers: `OPENAI_API_KEY`, `GOOGLE_API_KEY` (or `GEMINI_API_KEY`), or the variable named by the model's `api_key_env`.

### `Model '...' needs an API key. Set OPENAI_API_KEY`

The same for OpenAI-style providers; the message names the variable.

### A hosted provider fails and I cannot tell why

The Anthropic, OpenAI and Gemini providers have not been run against the live services by the project's authors. If one misbehaves, run `uv run agentkit models test <role>` and `uv run agentkit doctor`, and please open an issue with the output; see [Contributing](../CONTRIBUTING.md).

### `The model did not produce JSON matching the schema in 3 attempts`

The agent has an `output_schema`, the model has no native structured output (declared `structured_output: false`), and it could not produce valid JSON after three attempts. Simplify the schema, use a more capable model, or check the model's declared capabilities.

### `Model '...' does not support tool calling`

An agent has tools but its model's capabilities say `tools: false`. Use a model that supports tools, or correct the declared capability if it is wrong.

### The model answers without calling the tool

Small models, and sometimes large ones, do the work in their heads. Make the tool's docstring say when to use it ("Use this for any multiplication instead of working it out yourself"), say so in the agent's prompt, and measure it with an eval that has `tool_calls: must_include`. If a model keeps doing it, it is probably the wrong model for this agent.

### A local model's output is cut off or it forgets the conversation

The model's real context window is probably larger than the 8,192 tokens the kit assumes, or smaller than you think. Declare it under `capabilities.context_window` for `local/default`; see [Models and providers](models.md#declare-what-your-model-supports). Conversation trimming depends on it.

## Runs

### `budget_exceeded`

The run reached a limit, and the error names it, for example `limits.max_steps (25 of 25)`. Either the task needs more room (raise the limit in the config or on the `AgentSpec`), or the agent is wandering (read its trace, and tighten the prompt or its tools). See [Guardrails](guardrails.md#budgets).

### `stalled`

The agent repeated the same tool call with the same arguments more than three times, or produced two empty turns in a row. Open the trace. A repeated call usually means a tool keeps returning something the model cannot use, which is a tool-description or error-message problem.

### A tool returns `permission`, and I did not expect that

There are several causes, and the error message says which:

- **`The operator did not approve this X call.`** The tool is side-effecting and the policy is `require`. In an interactive run you were asked and said no. In an unattended one, such as an eval, a library `RunContext` with no channel, or `tools call`, the channel is `auto_deny` and the note says `This run is unattended`. Allow the tool in `tools.approval.overrides` if you trust it, or give the run a channel that can ask.
- **`X is denied by policy.`** The policy is `deny` for that tool or class.
- **`The domain 'x' is not in the allowed list`.** `http_fetch` fetches nothing until `tools.settings.http_fetch.allowed_domains` is set.
- **`'x' is outside the file workspace`.** The file tools only reach inside `tools.settings.filesystem.root`.
- **`The output of X was withheld by a check`.** A check you added rejected it.

### `The file workspace does not exist`

The file tools work in `./workspace` (relative to the project) by default. Create the folder, or point `tools.settings.filesystem.root` at one.

### `No tool named 'x' is registered. Registered: ...`

An agent's `tools` lists a name that is not available. Either it is a builtin that is not in `tools.enabled`, or the name is misspelled, or a project tool's file did not define it. `agentkit tools list --project-dir <dir>` shows what is registered.

### `No agent named 'x'. Defined agents: ...`

`agentkit run` takes the name inside the `AgentSpec` or pattern (`name="my_agent"`), not the variable name or the file name.

### The agent sees `[redacted:email]` where it needed an email address

The redactor, on by default for tool output and the final answer, removed it. If a tool must hand such values to the model, change `guardrails.redact_at` (remove `tool_output`, or set it to `[]`). See [Guardrails](guardrails.md#the-redactor).

### A run was fine yesterday and fails today

Check `agentkit trace` for the failing span. Common causes are a provider outage or rate limit (the kit retries, then falls back if you configured a fallback), a local server that was stopped, and a changed prompt or tool. Run the eval suite and `eval compare` it with an earlier run.

## Tools

### A tool I wrote is not found, or is not described the way I expect

Project tools must be in `tools/*.py` of the project, decorated with `@tool`. Files starting with `_` are skipped. A tool needs a docstring, type hints on every argument, and a snake_case name. Check `agentkit tools describe <name> --project-dir <dir>` to see exactly what the model is told.

### `Argument 'x' of tool 'y' needs a type hint` or `has no docstring`

The decorator derives the schema from type hints and the description from the docstring, and refuses a tool missing either. Add them.

### MCP: `The MCP server 'x' ... could not be reached`

The server did not start or did not answer. The message ends with the underlying cause. For a local server, run its `command` yourself in a terminal to see why it fails, and check that `pass_env` includes any variable it needs. For an HTTP server, check the URL and `headers_env`. Remember that every command that loads tools starts the configured servers.

### MCP tools always need approval

On purpose. A server's own claims about its tools are not trusted, so an MCP tool is `write` until you declare it in `tools.mcp_servers.<name>.side_effects`, for example `search: read_external`.

## Evals

### `No eval suites (*.yaml, *.jsonl) found in evals`

`eval run` looks in `evals.dataset_dir`, relative to the project. Pass a path, or check `--project-dir`.

### `X has no fixture in this eval`

A case used a side-effecting tool, or an external tool, that has no fixture, and the run is not `--live`. Add a fixture for it under `evals/fixtures/` and list it in the case's `setup.fixtures`. See [Writing evals](writing-evals.md#fixtures).

### `The cassette ... has no recorded response for this request`

You ran `--replay` after changing something that goes into the request: the prompt, a tool description, a fixture result or the input. Record again with `--record`. A tool that returns something that varies, such as the current time, cannot be replayed; give it a fixture.

### A case passes with scripted replies and fails with the real model

That is what the real-model run is for. Open the failing attempt's trace (the report prints its run ID) to see what the model did differently. Scripted cases test the wiring; they do not test the model.

### `llm_judge` is skipped

When a case scripts the model with `fake_model`, the judge is the fake provider and cannot judge, so the grader is skipped and counted neither way. The report lists skipped graders. Remove `fake_model`, or use `--record`, to run it for real.

### An eval prints warnings I did not expect

A case that deliberately triggers a failure, such as a denied write, logs it as a warning on stderr. The report on stdout is unaffected.

## Sessions

### `A session needs an ID`

You called the session functions with a `RunContext` that has no `session_id`. Create it with `RunContext(cfg, session_id="...")`.

### The chat forgot what we said

The history is kept per session ID. Check you passed the same `--session`, and that `memory.store` is `sqlite` (with `memory`, history lasts only as long as the process). If the conversation is long, older tool results may have been blanked or summarised; see [Sessions and chat](sessions.md#when-the-conversation-gets-too-long).

## Platform and installation

- **Python 3.11 or newer** is required. uv installs it for you.
- **Platforms.** Development and testing so far were on macOS. The CI workflow targets Linux. Windows has not been tried. Reports are welcome.
- **The `anthropic`, `adk` and `mcp` extras** are optional. A feature that needs one tells you which to install.
- **The package is not on PyPI.** Install from the repository, as the [README](../README.md#install) describes.

## FAQ

**Do I need an API key to try it?** No. `agentkit new`, the starter evals, and every example run with no key. Use a local model, or a key, only when you want a real model.

**Can I use my own model server?** Yes, anything that speaks the OpenAI chat-completions protocol: LM Studio, Ollama, vLLM, llama.cpp, a gateway. See [Models and providers](models.md).

**Is my data sent anywhere?** Only to the model provider you configure, and to services your tools call (`http_fetch` targets, a SearXNG instance, MCP servers). With a local model and no such tools, nothing leaves your machine. The kit has no telemetry of its own.

**How do I stop an agent from doing something risky?** Declare tools honestly (`side_effects`), keep the shipped approval policy, set tight limits, and give each agent only the tools it needs. [Guardrails](guardrails.md) and [SECURITY.md](../SECURITY.md) explain the layers.

**Does it replace LangChain, LangGraph or Google ADK?** No. It supplies the parts around an agent loop: models, tools, limits, evals and traces. It has a small loop of its own and an adapter for ADK, and its design keeps the underlying library visible.

**Why is the model catalogue so short?** Model IDs change faster than the kit does, so only the Claude models and the local and fake entries ship. Adding another is a few lines of YAML; see [Models and providers](models.md#adding-a-model-from-another-provider).

**How do I change the model for one agent?** Set `model=` on its `AgentSpec` to a role, or use `<AGENT_NAME>_MODEL_PROVIDER=local` to move that agent to a local model.

**Where is the data stored?** In the project's `.agentkit/` folder: traces, eval results and the SQLite memory file. Add it to `.gitignore`; new projects do.

**How do I get help or report a bug?** Open an issue; see [CONTRIBUTING.md](../CONTRIBUTING.md). For a vulnerability, follow [SECURITY.md](../SECURITY.md) instead, and do not open a public issue.
