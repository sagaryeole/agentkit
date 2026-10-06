"""The whole configuration as one Pydantic model. Unknown keys are errors.

Values come from config/default.yaml and the sources layered over it, so most
fields have no Python default: the YAML is the single place defaults live.
"""

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

ApprovalPolicy = Literal["require", "allow", "deny"]
SideEffects = Literal["none", "read_external", "write", "destructive"]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class ModelCapabilities(_Section):
    tools: bool | None = None
    parallel_tool_calls: bool | None = None
    forced_tool_choice: bool | None = None
    structured_output: bool | None = None
    vision: bool | None = None
    streaming: bool | None = None
    prompt_caching: bool | None = None
    context_window: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)


class ModelEntry(_Section):
    provider: str
    model_id: str
    api_base: str | None = None  # openai, google, openai_compat: endpoint, including /v1
    api_key_env: str | None = None  # openai, google, openai_compat: variable holding the key
    defaults: dict[str, Any] = {}
    capabilities: ModelCapabilities | None = None


class LocalServer(_Section):
    server: Literal["lmstudio", "ollama", "custom"]
    api_base: str | None = None  # defaults from `server`
    api_key: str = "local"  # placeholder; local servers ignore it
    request_timeout_seconds: float = Field(gt=0)


class Models(_Section):
    # "local" moves every hosted role to the local model; anything else leaves roles as mapped.
    provider: str | None = None
    roles: dict[str, str]
    fallbacks: dict[str, list[str]] = {}
    models: dict[str, ModelEntry] = {}
    local: LocalServer

    @model_validator(mode="after")
    def _references_exist(self) -> Self:
        for role, key in self.roles.items():
            if key not in self.models:
                raise ValueError(f"role '{role}' points to '{key}', which is not in models.models")
        for role, chain in self.fallbacks.items():
            if role not in self.roles:
                raise ValueError(f"fallbacks.{role} is not a role in models.roles")
            for key in chain:
                if key not in self.models:
                    raise ValueError(f"fallbacks.{role} lists '{key}', not in models.models")
        return self


class Price(_Section):
    """USD per million tokens."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cached_input: float | None = Field(default=None, ge=0)


class Limits(_Section):
    max_steps: int = Field(gt=0)
    max_tool_calls: int = Field(ge=0)
    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    max_cost_usd: float = Field(ge=0)
    max_wall_seconds: float = Field(gt=0)


class Retry(_Section):
    max_attempts: int = Field(ge=1)
    base_delay_seconds: float = Field(ge=0)
    max_delay_seconds: float = Field(ge=0)


class Timeouts(_Section):
    model_call_seconds: float = Field(gt=0)
    tool_call_seconds: float = Field(gt=0)


class ToolApproval(_Section):
    default: ApprovalPolicy
    by_side_effects: dict[SideEffects, ApprovalPolicy] = {}
    overrides: dict[str, ApprovalPolicy] = {}  # by tool name


class Tools(_Section):
    enabled: list[str]
    max_tools_per_agent: int = Field(ge=1)  # more than this triggers a warning
    max_output_chars: int = Field(ge=1)  # longer tool output is truncated
    approval: ToolApproval
    # Per-tool settings, validated by the tool that reads them.
    settings: dict[str, dict[str, Any]] = {}


class Guardrails(_Section):
    approval_channel: Literal["cli", "callback", "auto_deny", "auto_approve"]
    redact_patterns: list[str] = []
    max_identical_tool_calls: int = Field(ge=1)  # the same call more often than this stalls the run
    max_empty_steps: int = Field(ge=1)  # consecutive steps with neither a tool call nor an answer


class Observability(_Section):
    exporter: Literal["jsonl", "otlp", "console", "none"]
    jsonl_path: str
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    capture_content: bool


class Evals(_Section):
    dataset_dir: str
    results_dir: str
    cassette_dir: str
    concurrency: int = Field(ge=1)
    judge_role: str
    judge_min_score: float = Field(ge=0, le=1)  # an llm_judge score below this fails
    fail_under: float = Field(ge=0, le=100)  # `eval run` fails below this pass rate, in percent
    max_pass_rate_drop: float = Field(ge=0)  # `eval compare` fails beyond this drop, in points


class ContextSettings(_Section):
    """What to do when a conversation nears the model's context window."""

    strategy: Literal["off", "drop_tool_results", "summarize"]
    trigger_ratio: float = Field(gt=0, le=1)  # act at this share of the context window
    keep_recent_messages: int = Field(ge=1)  # the newest messages are never touched


class Memory(_Section):
    store: Literal["memory", "sqlite"]  # where sessions and state are kept
    sqlite_path: str
    context: ContextSettings


class Runtime(_Section):
    adapter: Literal["native", "adk", "langgraph", "claude_sdk"]


class Config(_Section):
    env: str
    models: Models
    pricing: dict[str, Price] = {}
    limits: Limits
    retry: Retry
    timeouts: Timeouts
    tools: Tools
    guardrails: Guardrails
    observability: Observability
    evals: Evals
    memory: Memory
    runtime: Runtime

    @model_validator(mode="after")
    def _cross_checks(self) -> Self:
        if self.env == "prod" and self.guardrails.approval_channel == "auto_approve":
            raise ValueError("guardrails.approval_channel 'auto_approve' is not allowed in prod")
        if self.evals.judge_role not in self.models.roles:
            raise ValueError(f"evals.judge_role '{self.evals.judge_role}' is not in models.roles")
        return self
