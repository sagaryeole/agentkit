# Agentic Development Kit: Specification

A reusable foundation for building AI agent projects: shared tools, an eval harness, a provider-independent model layer, configuration, guardrails and observability, plus a template for starting new agents.

This document is both a specification and a build prompt. Hand it to a coding agent or a developer to create the project from scratch.

Working name: `agentkit`. Replace it throughout with your own name.

---

## 1. How to use this document

If you are a coding agent building this project:

1. Read sections 2 to 4 before writing code. They define the constraints every later section depends on.
2. Build in the order given in section 17. Each milestone must pass its acceptance criteria before the next one starts.
3. Where this document says "decision", the choice is made. Where it says "open choice", ask the project owner before proceeding.
4. Do not add features that are not in this document. Section 3 lists what is deliberately left out.

---

## 2. Goals

1. A new agent project starts from a working baseline in under ten minutes.
2. Tools, evals, model access, configuration and safety controls are written once and reused across agents.
3. The model provider can be changed by editing configuration, with no change to agent code.
4. The underlying agent library (Google ADK, LangGraph, Claude Agent SDK, or none) can be changed by swapping one adapter.
5. Every agent built on the kit is testable, observable and bounded in cost from its first run.

## 3. Non-goals

1. This is not a new agent runtime. It does not reimplement the agent loop where an existing library already provides one.
2. It does not hide the underlying library. Developers can always reach the native objects.
3. It does not include a UI, a hosted service, a vector database, or a prompt-management product.
4. It does not provide multi-tenant auth, billing or user management.
5. It does not try to support every provider feature. Provider-specific features are reachable through an escape hatch (section 7.5), not modelled in the common interface.

## 4. Design principles

These are binding. When a design question comes up that this document does not answer, resolve it with these.

1. **Thin over complete.** Each module should be small enough to read in one sitting. Prefer a function to a class and a class to a hierarchy.
2. **Extract, do not invent.** An abstraction is added only when two real agents need the same thing. Anything speculative goes in the backlog, not the code.
3. **Agent library stays visible.** Adapters translate; they do not wrap. No kit type should need to exist purely to conceal a library type.
4. **Configuration over code.** Model choice, limits, timeouts, retry policy and tool enablement live in config files, never in prompts or agent code.
5. **Safe by default.** A tool has side effects unless declared read-only. A side-effecting tool needs approval unless configuration grants it. Every run has a budget.
6. **Everything is traceable.** Each run has an ID. Each model call and tool call is recorded with inputs, outputs, duration, tokens and cost.
7. **Evals are part of the build.** An agent without an eval set is not finished. Changing a prompt, tool or model requires re-running evals.
8. **Workflow before agent, one agent before many.** Use fixed steps when the steps are known. Use one agent with good tools before adding more agents.

---

## 5. Technical baseline

| Item | Decision |
|---|---|
| Language | Python 3.11 or newer |
| Packaging | `uv` with `pyproject.toml`; `src/` layout |
| Data validation | Pydantic v2 |
| Async | `asyncio` throughout; sync wrappers only at the CLI boundary |
| Config format | YAML files plus environment variables |
| Testing | `pytest`, `pytest-asyncio` |
| Lint and types | `ruff`, `mypy --strict` on the kit package |
| Tracing | OpenTelemetry API, with a JSON-lines file exporter as the default |
| CLI | `typer` |
| Licence | Open choice: ask the project owner |

Open choice: if the owner prefers TypeScript, keep every interface in this document and translate idioms (Zod for Pydantic, `vitest` for `pytest`). The module boundaries do not change.

---

## 6. Repository layout

```
agentkit/
├── pyproject.toml
├── README.md
├── config/
│   ├── default.yaml            # shipped defaults
│   ├── models.yaml             # model catalogue and role aliases
│   └── pricing.yaml            # per-model token prices for cost tracking
├── src/agentkit/
│   ├── config/                 # section 8
│   ├── models/                 # section 7: provider layer
│   │   ├── base.py
│   │   ├── registry.py
│   │   └── providers/          # one file per provider
│   ├── tools/                  # section 9
│   │   ├── base.py
│   │   ├── registry.py
│   │   └── builtin/            # ready-made tools
│   ├── guardrails/             # section 10: approvals, budgets, policies
│   ├── runtime/                # section 11: run context, adapters
│   │   └── adapters/           # adk.py, langgraph.py, claude_sdk.py, native.py
│   ├── memory/                 # section 12: sessions and state
│   ├── observability/          # section 13
│   ├── evals/                  # section 14
│   ├── patterns/               # section 15: reusable agent shapes
│   └── cli/                    # section 16
├── templates/
│   └── agent/                  # scaffold copied by `agentkit new`
├── examples/
│   ├── single_agent/
│   ├── router/
│   └── human_in_loop/
├── tests/
│   ├── unit/
│   ├── integration/            # real provider calls, opt-in
│   └── fakes/                  # fake model and fake tools
└── docs/
    ├── patterns.md
    ├── writing-tools.md
    └── writing-evals.md
```

---

## 7. Model provider layer

### 7.1 Purpose

Agent code asks for a model by role ("default", "fast", "reasoning"). Configuration maps the role to a provider and model ID. Changing provider is a config edit.

### 7.2 Core types

```python
class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: list[ContentPart]          # text, image, tool_call, tool_result

class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict                  # JSON Schema

class ModelRequest(BaseModel):
    messages: list[Message]
    tools: list[ToolSpec] = []
    tool_choice: Literal["auto", "required", "none"] | str = "auto"
    max_output_tokens: int | None = None
    temperature: float | None = None
    response_schema: dict | None = None  # structured output
    stop: list[str] = []
    provider_options: dict = {}          # escape hatch, section 7.5

class Usage(BaseModel):
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    cost_usd: float | None = None

class ModelResponse(BaseModel):
    message: Message
    stop_reason: Literal["end", "tool_use", "max_tokens", "stop_sequence", "refusal", "error"]
    usage: Usage
    model_id: str
    raw: Any                             # untouched provider response
```

### 7.3 Provider interface

```python
class ModelProvider(Protocol):
    name: str
    capabilities: Capabilities

    async def generate(self, req: ModelRequest) -> ModelResponse: ...
    def stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]: ...
    async def count_tokens(self, req: ModelRequest) -> int: ...
```

`Capabilities` declares what the model supports: tool calling, parallel tool calls, structured output, vision, streaming, prompt caching, context window size, maximum output tokens. The kit checks a request against capabilities before sending and raises `UnsupportedCapability` with a clear message instead of letting the provider return an obscure error.

### 7.4 Providers to ship

| Provider | Requirement |
|---|---|
| Anthropic | Required |
| Google Gemini | Required |
| OpenAI | Required |
| OpenAI-compatible endpoint | Required; covers gateways and other hosted endpoints |
| Local (Ollama, LM Studio) | Required; built on the OpenAI-compatible provider, see section 7.8 |
| Fake | Required; scripted responses for tests and evals, no network |

Each provider is one file implementing `ModelProvider`. Provider SDKs are optional dependencies (`agentkit[anthropic]`, `agentkit[google]`), so installing the kit does not pull every SDK.

### 7.5 Escape hatch

`ModelRequest.provider_options` is passed through to the provider untouched. `ModelResponse.raw` holds the original response. Provider-specific features are used this way; they are not added to the common types until at least two providers support them.

### 7.6 Cross-cutting behaviour

Implemented once, as wrappers around any provider:

1. **Retry** with exponential backoff and jitter on rate limits, timeouts and 5xx errors. Never retry on 4xx validation errors.
2. **Timeout** per call, from config.
3. **Fallback chain**: an ordered list of models tried when the first fails after retries.
4. **Cost calculation** from `pricing.yaml`, written into `Usage.cost_usd`.
5. **Tracing** span per call (section 13).
6. **Budget check** before each call (section 10.3).

### 7.7 Model catalogue

`config/models.yaml`:

```yaml
roles:
  default:   anthropic/claude-sonnet-5-5
  fast:      anthropic/claude-haiku-4-5
  reasoning: anthropic/claude-opus-5-5
  judge:     anthropic/claude-sonnet-5-5   # used by eval graders

fallbacks:
  default: [openai/<model-id>, google/<model-id>]

models:
  anthropic/claude-sonnet-5-5:
    provider: anthropic
    model_id: claude-sonnet-5-5
    defaults: { max_output_tokens: 4096 }
```

Model IDs are examples. Verify current IDs with each provider at build time; do not hard-code them anywhere except this file.

Usage in agent code:

```python
model = get_model("default")           # by role
model = get_model("openai/<model-id>") # by explicit key, discouraged
```

### 7.8 Local models

Any agent must be able to run on a model served on the developer's machine by Ollama or LM Studio, with no change to agent code and no cloud account.

**Approach.** Both servers expose an OpenAI-compatible API, so the `local` provider is a thin preset over the OpenAI-compatible provider. It adds defaults, discovery and capability handling; it does not implement a separate protocol.

| Server | Default base URL |
|---|---|
| LM Studio | `http://127.0.0.1:1234/v1` |
| Ollama | `http://127.0.0.1:11434/v1` |

Any other server that speaks the same protocol (llama.cpp server, vLLM and similar) works by setting the base URL.

**Configuration.**

```yaml
models:
  provider: local                 # global switch: hosted roles are remapped to local
  roles:
    default: local/default

  local:
    server: lmstudio              # lmstudio | ollama | custom
    api_base: http://127.0.0.1:1234/v1   # optional; defaults from `server`
    api_key: local                # placeholder; local servers ignore it
    request_timeout_seconds: 300  # local generation is slower

  models:
    local/default:
      provider: local
      model_id: <id-reported-by-the-server>
      capabilities:
        tools: true
        structured_output: false
        vision: false
        context_window: 8192
```

Environment variables, matching the convention already used in the ADK tutorial project:

| Variable | Meaning |
|---|---|
| `MODEL_PROVIDER` | Global switch between a hosted provider and `local` |
| `<AGENT_NAME>_MODEL_PROVIDER` | Override for a single agent |
| `LOCAL_API_BASE` | Base URL, including the port and `/v1` |
| `LOCAL_MODEL_ID` | Exact model ID as reported by the server |
| `LOCAL_API_KEY` | Optional; a placeholder is sent when unset |

**Requirements.**

1. **Global and per-agent switch.** One setting moves every role to the local model; a per-agent override moves a single agent either way. This lets a multi-agent system mix local and hosted models.
2. **Discovery.** `agentkit models list --local` calls `GET {api_base}/models` and prints the IDs the server currently offers.
3. **Clear start-up errors.** Before the first call the provider checks that the server is reachable and that the configured model ID is in the server's list. Failures name the cause: server not running, base URL missing the port or `/v1`, or model ID not loaded, with the available IDs listed.
4. **Capabilities are declared per model.** Local models differ widely in tool calling, structured output and context size, so these cannot be assumed from the provider. The kit ships conservative defaults and lets config override them.
5. **Degraded modes.** When a model lacks native structured output, the kit falls back to prompting for JSON, validating against the schema, and retrying a bounded number of times. When a model lacks tool calling, the agent fails at start-up with a clear message; it does not silently run without tools.
6. **Small context windows.** The context management strategy in section 12 reads the model's declared `context_window`, so history is trimmed earlier for local models.
7. **Cost.** Local models are priced at zero in `pricing.yaml`. Token counts, step limits and wall-time limits still apply.
8. **No key required.** A run with `provider: local` must work with no cloud credentials present.
9. **ADK adapter.** The ADK adapter maps a local model to ADK's LiteLLM model class using the `openai/<model_id>` form with `api_base`, which is how the tutorial project already does it.
10. **Fallback in both directions.** A fallback chain may go from local to hosted or hosted to local. Falling back from local to hosted sends data off the machine, so it is off unless the chain is configured explicitly.

**Local models in evals.** Running the same suite on a local and a hosted model, then using `agentkit eval compare`, is the supported way to decide whether a local model is good enough for an agent. The judge model is configured separately and may stay hosted.

---

## 8. Configuration

### 8.1 Load order

Later sources override earlier ones:

1. `config/default.yaml` shipped with the kit
2. Project `agentkit.yaml`
3. Environment-specific `agentkit.<env>.yaml`, selected by `AGENTKIT_ENV`
4. Environment variables prefixed `AGENTKIT_`, with `__` as the nesting separator
5. Arguments passed in code or on the CLI

### 8.2 Schema

The whole config is one Pydantic model, validated at start-up. Unknown keys are errors.

```yaml
env: dev

models:                 # see section 7.7
  roles: { ... }

limits:
  max_steps: 25                 # model turns per run
  max_tool_calls: 50
  max_input_tokens: 500000
  max_output_tokens: 50000
  max_cost_usd: 2.00
  max_wall_seconds: 300

retry:
  max_attempts: 3
  base_delay_seconds: 1.0
  max_delay_seconds: 30.0

timeouts:
  model_call_seconds: 120
  tool_call_seconds: 60

tools:
  enabled: [http_fetch, read_file, calculator]
  approval:
    default: require            # require | allow | deny
    overrides:
      read_file: allow
      send_email: require
  settings:
    http_fetch:
      allowed_domains: ["*.example.com"]
    filesystem:
      root: ./workspace

guardrails:
  approval_channel: cli         # cli | callback | auto_deny
  redact_patterns: [email, api_key]

observability:
  exporter: jsonl               # jsonl | otlp | console | none
  jsonl_path: ./.agentkit/traces
  log_level: INFO
  capture_content: true         # false stores metadata only

evals:
  dataset_dir: ./evals
  concurrency: 4
  judge_role: judge

runtime:
  adapter: native               # native | adk | langgraph | claude_sdk
```

### 8.3 Secrets

1. Secrets come only from environment variables or a pluggable `SecretProvider` (env by default; interfaces for a cloud secret manager).
2. Secrets never appear in YAML, traces, logs or eval outputs. The observability layer redacts known secret values before writing.
3. A `.env.example` lists every variable the kit reads. `.env` is git-ignored.

---

## 9. Tools

### 9.1 Tool contract

```python
@tool(
    name="http_fetch",
    side_effects="none",            # none | read_external | write | destructive
    idempotent=True,
    timeout_seconds=30,
    tags=["web"],
)
async def http_fetch(url: HttpUrl, max_bytes: int = 200_000, *, ctx: RunContext) -> FetchResult:
    """Fetch a web page and return its text content.

    Use this when you need the current contents of a specific URL.
    Do not use it to search; use `web_search` for that.

    Args:
        url: Absolute http or https URL.
        max_bytes: Truncate the body after this many bytes.
    """
```

Rules:

1. Input schema is derived from the type hints. Output is a Pydantic model or a plain JSON-serialisable value.
2. The docstring is written for the model: what the tool does, when to use it, when not to, and what each argument means.
3. `side_effects` is mandatory. It defaults to `write` if omitted, so an undeclared tool is treated as risky.
4. `ctx` is injected and gives access to config, the run ID, the budget, a logger and a state store. It is not shown to the model.
5. A tool never raises to the model. Failures return a `ToolError` with a `kind` (`invalid_input`, `not_found`, `permission`, `timeout`, `transient`, `internal`), a message the model can act on, and `retryable: bool`.
6. Large outputs are truncated to a configurable limit with an explicit truncation marker and, where possible, a way to fetch the rest.
7. Tool names are `snake_case` verbs, unique within the registry.

### 9.2 Registry

```python
registry = ToolRegistry.from_config(cfg)     # loads enabled builtin tools
registry.register(my_tool)
registry.load_mcp("server-name")             # tools from an MCP server
tools = registry.select(tags=["web"])        # subset for one agent
```

The registry rejects duplicate names, and warns when an agent is given more than a configurable number of tools (default 20), because tool selection quality drops as the list grows.

### 9.3 Ready-made tools

Ship these in `tools/builtin/`. Each has unit tests and at least one eval case.

| Tool | Side effects | Notes |
|---|---|---|
| `calculator` | none | Safe expression evaluation, no `eval` |
| `current_time` | none | Time zone aware |
| `read_file` | none | Confined to the configured root |
| `list_files` | none | Confined to the configured root |
| `search_files` | none | Text search within the root |
| `write_file` | write | Confined to the root; approval by default |
| `http_fetch` | read_external | Domain allowlist; blocks private IP ranges |
| `web_search` | read_external | Pluggable search backend behind one interface |
| `sql_query` | none | Read-only connection; row and time limits |
| `run_python` | write | Sandboxed execution only; disabled by default |
| `ask_human` | none | Asks the operator a question and waits |
| `handoff` | none | Transfers control to a named agent |
| `remember` / `recall` | write / none | Session-scoped key-value notes |
| `finish` | none | Declares the task complete with a structured result |

Open choice: which search backend and which sandbox to implement first.

### 9.4 Tool safety requirements

1. Filesystem tools resolve paths and refuse anything outside the configured root, including through symbolic links.
2. `http_fetch` refuses non-http schemes, loopback, link-local and private address ranges, and follows redirects only to allowed domains.
3. `sql_query` uses a read-only role and rejects statements that are not a single `SELECT`.
4. Tool output is untrusted input to the model. Document this in `docs/writing-tools.md` and wrap external content in a clearly delimited block so injected instructions are less likely to be followed.

### 9.5 MCP

The registry can load tools from Model Context Protocol servers declared in config. MCP tools go through the same approval, timeout, tracing and truncation path as native tools. An MCP tool with no declared side effects is treated as `write`.

---

## 10. Guardrails

### 10.1 Approval gate

Before a tool with side effects runs, the runtime consults the approval policy:

```python
class ApprovalRequest(BaseModel):
    run_id: str
    tool_name: str
    arguments: dict
    side_effects: str
    reason: str | None            # the model's stated reason, if any

class ApprovalDecision(BaseModel):
    outcome: Literal["approve", "deny", "edit"]
    edited_arguments: dict | None = None
    note: str | None = None

class ApprovalChannel(Protocol):
    async def request(self, req: ApprovalRequest) -> ApprovalDecision: ...
```

Channels shipped: `cli` (interactive prompt), `callback` (user-supplied function, for web or chat integrations), `auto_deny` (for unattended runs and evals), `auto_approve` (only for tests; refuses to load when `env` is `prod`).

A denial is returned to the model as a `ToolError` of kind `permission`, so the agent can choose another path. Every decision is recorded in the trace.

Approvals must be resumable: a run waiting on approval can be serialised and continued later.

### 10.2 Policy

Policy is resolved per tool, most specific first: explicit override in config, then the default for the tool's `side_effects` class, then the global default. Destructive tools can never be set to `allow` without an explicit per-tool override.

### 10.3 Budgets

A `Budget` object lives on the run context and tracks steps, tool calls, input tokens, output tokens, cost and wall time against `limits` in config. It is checked before every model call and every tool call. On breach the run stops with `stop_reason="budget_exceeded"`, names the limit that was hit, and returns whatever partial result exists.

### 10.4 Loop and stall detection

The runtime stops a run when the same tool is called with identical arguments more than a configurable number of times (default 3), or when a configurable number of consecutive steps produce no tool call and no final answer.

### 10.5 Input and output checks

A small hook interface, with nothing heavyweight built in:

```python
class Check(Protocol):
    async def __call__(self, text: str, ctx: RunContext) -> CheckResult: ...
```

Hooks run at three points: user input, tool output, final answer. Ship two: a secret and personal-data redactor driven by `redact_patterns`, and an output schema validator. Others are added by projects.

---

## 11. Runtime

### 11.1 Run context

```python
class RunContext:
    run_id: str
    session_id: str | None
    config: Config
    budget: Budget
    tracer: Tracer
    state: StateStore
    approvals: ApprovalChannel
    logger: Logger
```

### 11.2 Agent definition

A declarative description, independent of any library:

```python
class AgentSpec(BaseModel):
    name: str
    description: str
    instructions: str                 # or a path to a prompt file
    model: str = "default"            # role or explicit key
    tools: list[str] = []
    sub_agents: list[str] = []
    output_schema: type[BaseModel] | None = None
    limits: Limits | None = None      # overrides global limits
```

Prompts live in files under `prompts/`, not in Python strings, and support simple variable substitution. Prompt files are versioned with the code.

### 11.3 Result

```python
class RunResult(BaseModel):
    run_id: str
    output: Any
    stop_reason: Literal["completed", "budget_exceeded", "stalled", "denied", "error", "awaiting_approval"]
    usage: Usage                       # totals for the run
    steps: int
    trace_path: str | None
    error: ErrorInfo | None
```

### 11.4 Adapters

```python
class RuntimeAdapter(Protocol):
    async def run(self, spec: AgentSpec, input: str | list[Message], ctx: RunContext) -> RunResult: ...
    def stream(self, spec: AgentSpec, input, ctx: RunContext) -> AsyncIterator[RunEvent]: ...
```

| Adapter | Requirement |
|---|---|
| `native` | Required. A minimal tool-calling loop on the provider layer, about 200 lines. It is the reference implementation and the default for tests and evals. |
| `adk` | Required. Translates `AgentSpec` and kit tools to Google ADK objects. |
| `langgraph` | Optional. |
| `claude_sdk` | Optional. |

Every adapter must route tool calls through the kit's tool executor, so approvals, budgets, timeouts and tracing apply regardless of the library. An adapter that cannot do this for some feature must document the gap.

Each adapter exposes `to_native(spec)`, returning the library's own agent object for developers who need it.

### 11.5 Tool executor

One function used by all adapters. In order: validate arguments, check approval, check budget, start a trace span, run with timeout, convert exceptions to `ToolError`, truncate output, run output checks, record usage, end the span.

---

## 12. Sessions and state

1. `StateStore` interface with `get`, `set`, `delete`, `list`, scoped to run, session or user.
2. `SessionStore` interface for conversation history: `append`, `load`, `truncate`.
3. Implementations shipped: in-memory and SQLite. Interfaces for others.
4. Context management: when history approaches the model's context window, apply a configurable strategy, either dropping oldest tool results first or summarising older turns with the `fast` model. The strategy and thresholds are in config.
5. Long-term memory and retrieval are out of scope for the first version, except the `remember` and `recall` tools.

---

## 13. Observability

### 13.1 Trace structure

```
run
├── model_call   (model_id, tokens, cost, stop_reason, latency)
├── tool_call    (tool, arguments, result or error, latency, approval decision)
├── model_call
└── ...
```

Sub-agent runs are child spans of the calling run. Every span carries `run_id`, `session_id`, `agent_name` and `env`.

### 13.2 Requirements

1. Use the OpenTelemetry API so any OTLP-compatible backend works.
2. Default exporter writes one JSON-lines file per run to `.agentkit/traces/`. No external service is needed to get traces.
3. `capture_content: false` records metadata only, for sensitive deployments.
4. Redaction runs before export.
5. Structured logs carry the `run_id` so logs and traces can be joined.
6. A run summary is printed at the end of each CLI run: steps, tool calls, tokens, cost, duration, stop reason.

### 13.3 Trace viewer

`agentkit trace <run_id>` prints a readable timeline in the terminal. No web UI in the first version.

---

## 14. Evals

### 14.1 Purpose

Answer one question reliably: after this change to a prompt, tool or model, is the agent better, worse or the same?

### 14.2 Dataset format

One YAML or JSONL file per suite under `evals/`:

```yaml
suite: support_triage
agent: triage_agent
defaults:
  graders: [no_error, within_budget]

cases:
  - id: refund_simple
    input: "I was charged twice for order 1182."
    setup:
      fixtures: [orders_small]          # seeds fake tools
    expect:
      tool_calls:
        must_include: [lookup_order]
        must_not_include: [issue_refund] # needs approval first
      output:
        contains: ["1182"]
        schema: TriageResult
        fields: { category: billing }
    graders:
      - type: llm_judge
        rubric: "Reply acknowledges the duplicate charge and states the next step."
    tags: [billing, smoke]
```

### 14.3 Graders

| Grader | Checks |
|---|---|
| `exact`, `contains`, `regex` | Final output text |
| `schema` | Output validates against a model |
| `fields` | Named fields in structured output equal expected values |
| `tool_calls` | Required, forbidden and ordered tool calls, with optional argument matching |
| `trajectory` | Step count and tool sequence against a reference |
| `no_error` | Run ended with `completed` |
| `within_budget` | Steps, tokens, cost and time under stated limits |
| `llm_judge` | A rubric scored by the `judge` model, returning a score and a reason |
| `custom` | A Python function `(case, result) -> GradeResult` |

LLM-judge requirements: rubric is explicit and single-purpose; the judge returns structured output; the judge model is set separately from the agent's model; judge prompts are versioned.

### 14.4 Running

```
agentkit eval run evals/support_triage.yaml
agentkit eval run evals/ --tags smoke --repeat 3
agentkit eval compare <baseline_run> <candidate_run>
```

1. Cases run concurrently up to `evals.concurrency`.
2. `--repeat N` runs each case N times and reports the pass rate per case, because agent output varies between runs.
3. Tools are replaced by fakes or recorded fixtures by default, so evals are cheap and repeatable. A `--live` flag uses real tools.
4. Side-effecting tools always use `auto_deny` or fakes in evals unless a case explicitly allows them.
5. Results are written as JSON with full traces, so a failure can be inspected.

### 14.5 Reporting

A report shows, per suite: pass rate, per-grader pass rate, mean and p95 steps, tokens, cost and latency, and the list of failing cases with reasons. `compare` shows regressions and improvements case by case between two runs.

### 14.6 CI

A `smoke` tag marks a fast subset that runs on every change using the fake provider or recorded responses. The full suite runs on demand or on a schedule. A configurable threshold (for example, pass rate must not drop more than 2 points from baseline) fails the build.

### 14.7 Record and replay

Model responses can be recorded to cassette files and replayed, so most tests and smoke evals need no network and no spend.

---

## 15. Patterns

Shipped as small, documented building blocks in `patterns/`, each with an example and an eval suite. `docs/patterns.md` explains when to use each and when not to.

| Pattern | Use when |
|---|---|
| Single agent with tools | Default starting point |
| Fixed workflow (sequential steps) | The steps are known in advance |
| Router | Inputs fall into distinct categories handled differently |
| Planner and executor | The task needs a plan that is then carried out step by step |
| Parallel fan-out and merge | Independent subtasks can run at the same time |
| Evaluator and reviser loop | Output quality improves with a critique pass; bounded iterations |
| Human in the loop | Any step with side effects or low confidence |
| Hand-off between agents | One agent's context is overloaded or the skills are clearly separate |

`docs/patterns.md` must open with a decision guide: start with a fixed workflow if steps are known, otherwise a single agent, and add more agents only on evidence from evals.

---

## 16. CLI and scaffolding

```
agentkit new <name> [--pattern single|router|workflow]   # copy template
agentkit run <agent> "<input>" [--env dev] [--model fast]
agentkit chat <agent>                                    # interactive session
agentkit tools list | describe <tool> | call <tool> --args '{...}'
agentkit models list [--local] | test <role>             # --local queries the local server
agentkit config show | validate
agentkit eval run | compare | report
agentkit trace <run_id>
agentkit doctor                                          # checks keys, config, connectivity
```

### 16.1 Generated project

```
my-agent/
├── agentkit.yaml
├── .env.example
├── pyproject.toml
├── agents/
│   └── main.py              # AgentSpec definitions
├── prompts/
│   └── main.md
├── tools/                   # project-specific tools
├── evals/
│   ├── smoke.yaml           # three starter cases
│   └── fixtures/
├── tests/
└── README.md
```

A generated project must run and pass its starter evals immediately after `agentkit new`, using the fake provider, with no API key.

---

## 17. Build order and acceptance criteria

Build in this order. Do not start a milestone until the previous one passes.

**M1. Skeleton and config**
- Repo layout, packaging, lint, type checks and CI are in place.
- Config loads from all five sources in the stated order; an unknown key fails validation.
- `agentkit config validate` and `agentkit doctor` work.

**M2. Model layer**
- Fake provider, one hosted provider and the local provider (section 7.8) are implemented.
- Retry, timeout, fallback and cost calculation have unit tests using the fake provider.
- Switching the `default` role between two providers changes no code in an example agent.
- An example agent with one tool runs against both LM Studio and Ollama by changing only `server` and `model_id`.
- Stopping the local server, or setting a wrong model ID, produces the specific error messages required in section 7.8.

**M3. Tools**
- `@tool` decorator, registry and tool executor are implemented.
- Five builtin tools ship: `calculator`, `current_time`, `read_file`, `list_files`, `http_fetch`.
- Path-escape and private-address tests pass.

**M4. Native runtime and guardrails**
- The native adapter runs a single agent with tools end to end.
- Budgets, loop detection and the approval gate work, each with tests.
- A denied approval is visible to the model and the run continues.

**M5. Observability**
- Every run writes a JSON-lines trace; `agentkit trace` renders it.
- Redaction is verified by a test that plants a secret and checks it is absent from the trace.

**M6. Evals**
- Dataset loader, all graders, runner, report and compare are implemented.
- Record and replay works.
- The example agent has a suite that passes in CI with no network.

**M7. Scaffolding and remaining providers**
- `agentkit new` produces a project that passes its starter evals immediately.
- Remaining required providers and builtin tools are added.

**M8. ADK adapter and patterns**
- The ADK adapter runs the same `AgentSpec` and passes the same eval suite as the native adapter.
- Router, workflow and human-in-the-loop patterns each have an example and an eval suite.

**M9. Documentation**
- `README.md`, `docs/patterns.md`, `docs/writing-tools.md` and `docs/writing-evals.md` are complete.
- A new developer can follow the README from install to a passing custom eval.

---

## 18. Quality requirements

1. Unit tests need no network and no API keys. They use the fake provider and fake tools.
2. Integration tests against real providers are opt-in, marked, and skipped when keys are absent. Local-model tests are marked separately and skipped when no local server is reachable.
3. Line coverage of at least 85% on `models`, `tools`, `guardrails`, `runtime` and `evals`.
4. Public interfaces are fully typed and documented.
5. No module in the kit imports an agent library or provider SDK at top level outside its own adapter or provider file.
6. Errors raised by the kit derive from one `AgentKitError` base and say what to do next.
7. Semantic versioning. Breaking changes to `AgentSpec`, the tool contract, config schema or eval dataset format need a major version and a migration note.

---

## 19. Security requirements

1. Least privilege: an agent receives only the tools listed in its spec.
2. All content from tools, files, web pages and other agents is treated as untrusted data, never as instructions.
3. Side-effecting actions need approval by default.
4. Secrets are never logged, traced or sent to a model.
5. `auto_approve` and `run_python` without a sandbox refuse to load when `env` is `prod`.
6. Dependencies are pinned through a lock file and scanned in CI.
7. A `SECURITY.md` describes the threat model: prompt injection through tool output, excessive agency, data exfiltration through tools, and runaway cost.

---

## 20. Backlog (deliberately deferred)

Add only when two real projects need it:

- Long-term memory and retrieval
- Web UI for traces and evals
- Agent-to-agent protocol support across processes
- Distributed execution and queues
- Prompt optimisation tooling
- Online evaluation from production traffic
- Additional adapters and providers
- Deployment templates for specific clouds

---

## 21. Open choices for the project owner

Answer these before the build starts:

1. Project and package name.
2. Python or TypeScript.
3. Licence, and whether the kit is internal or public.
4. Which hosted provider to implement first in M2, and which local server (LM Studio or Ollama) is the default.
5. Search backend for `web_search`.
6. Sandbox technology for `run_python`, or drop the tool.
7. Trace backend beyond the local file exporter, if any.
8. Whether the LangGraph and Claude Agent SDK adapters are wanted.
